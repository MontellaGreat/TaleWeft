"""AI Invocation 会话与 attempt 服务。"""
from __future__ import annotations

import logging
import json
import hashlib
import uuid
import asyncio
from dataclasses import replace
from typing import Any, Callable, Mapping

from domain.ai.services.llm_service import GenerationConfig, LLMService
from application.ai.usage_context import capture_usage
from application.ai_invocation.continuation import ContinuationContext, execute_continuation
from application.ai_invocation.dtos import (
    AdoptionCommit,
    AdoptionCommitStatus,
    AdoptionCommitStep,
    AdoptionDecision,
    InvocationAttempt,
    InvocationAttemptStatus,
    InvocationPolicy,
    InvocationSession,
    InvocationSessionStatus,
    PromptSnapshot,
    prompt_hash,
    stable_hash,
)
from application.ai_invocation.output_binding_resolution import resolve_output_payload_value
from domain.ai.value_objects.prompt import Prompt
from application.ai_invocation.variable_hub import VariableHubRepository, VariableWrite
from application.engine.rules.anti_ai_stream_guard import AntiAIStreamGuard, is_prose_operation
from application.engine.rules.prose_prompt_hooks import ProsePromptHooks, archive_prompt_use, prompt_evidence_matches
from application.core.taxonomy.market_prompt import MarketPromptHooks, is_market_planning_operation
from application.ai_invocation.prose_execution_evidence import (
    ProseExecutionEvidenceError,
    verify_attempt_prose_receipts,
    verify_decision_prose_receipts,
)

logger = logging.getLogger(__name__)


class InvocationSessionService:
    """管理 invocation session 的最小服务。"""

    def __init__(self):
        self._sessions: dict[str, InvocationSession] = {}

    def create(
        self,
        *,
        operation: str,
        node_key: str,
        policy: InvocationPolicy,
        context,
        continuation=None,
        metadata=None,
    ) -> InvocationSession:
        session = InvocationSession(
            id=str(uuid.uuid4()),
            operation=operation,
            node_key=node_key,
            policy=policy,
            context=dict(context or {}),
            continuation=continuation,
            metadata=dict(metadata or {}),
        )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> InvocationSession:
        return self._sessions[session_id]

    def update_status(self, session: InvocationSession, status: InvocationSessionStatus) -> None:
        session.status = status

    def attach_prompt(self, session: InvocationSession, snapshot: PromptSnapshot, variable_plan) -> PromptSnapshot:
        if is_prose_operation(session.operation):
            hooks = AttemptService.prose_prompt_hook_factory()
            snapshot, evidence = hooks.freeze(snapshot, session.context)
            session.metadata = {**session.metadata, "anti_ai_prompt": evidence}
        elif is_market_planning_operation(session.operation):
            snapshot, evidence = AttemptService.market_prompt_hook_factory().freeze(snapshot, session.context)
            session.metadata = {**session.metadata, "market_profile_prompt": evidence}
        session.prompt_snapshot = snapshot
        session.variable_plan = variable_plan
        session.status = InvocationSessionStatus.PROMPT_COMPILED
        return snapshot


class AttemptService:
    """创建 attempt 并统一调用 LLMService。"""

    # Composition-root hook.  Capability status can inspect the installed
    # factory, while observed status still requires a terminal stream receipt.
    prose_stream_guard_factory = AntiAIStreamGuard
    prose_prompt_hook_factory = ProsePromptHooks
    market_prompt_hook_factory = MarketPromptHooks

    def _prepare_prose_prompt(self, session, snapshot):
        if not is_prose_operation(session.operation):
            if is_market_planning_operation(session.operation):
                type(self).market_prompt_hook_factory().validate_frozen(
                    snapshot.prompt, session.context, session.metadata.get("market_profile_prompt"))
            return snapshot, None
        evidence = session.metadata.get("anti_ai_prompt")
        hooks = type(self).prose_prompt_hook_factory()
        from application.core.services.chapter_derivation_readiness import assert_previous_derivations_ready
        assert_previous_derivations_ready(hooks.db, session.context.get('novel_id'), session.context.get('chapter_number') or 1)
        if not prompt_evidence_matches(snapshot.prompt, evidence):
            # Reviewed snapshots must be recompiled visibly before changing
            # what is sent to the provider. Direct low-level callers have no
            # pre-call review and may freeze their prompt here.
            if session.policy != InvocationPolicy.DIRECT:
                raise RuntimeError("去味提示词快照缺失或已改变，请重新编译后审阅")
            if isinstance(snapshot, PromptSnapshot):
                snapshot, evidence = hooks.freeze(snapshot, session.context)
            else:
                # Lightweight integrations still retain the exact sent prompt.
                from types import SimpleNamespace
                prompt, evidence = hooks.compose(snapshot.prompt, session.context)
                snapshot = SimpleNamespace(prompt=prompt)
            session.prompt_snapshot = snapshot
            session.metadata = {**session.metadata, "anti_ai_prompt": evidence}
        hooks.validate_current_market_profile(evidence, session.context)
        return snapshot, hooks

    def __init__(self, llm_service: LLMService):
        self._llm_service = llm_service
        self._attempts: dict[str, InvocationAttempt] = {}

    async def generate(
        self,
        *,
        session: InvocationSession,
        prompt_snapshot: PromptSnapshot,
        config: GenerationConfig | None = None,
    ) -> InvocationAttempt:
        prompt_snapshot, prompt_hooks = self._prepare_prose_prompt(session, prompt_snapshot)
        attempt = InvocationAttempt(
            id=str(uuid.uuid4()),
            session_id=session.id,
            status=InvocationAttemptStatus.RUNNING,
            prompt_snapshot=prompt_snapshot,
        )
        self._attempts[attempt.id] = attempt
        session.attempts.append(attempt.id)
        session.status = InvocationSessionStatus.GENERATING
        try:
            with capture_usage(novel_id=str(session.context.get('novel_id') or ''), attempt_id=attempt.id) as usage:
                try:
                    result = await self._llm_service.generate(prompt_snapshot.prompt, config or GenerationConfig())
                finally:
                    attempt.accounting = dict(usage.metadata)
                    attempt.token_usage = usage.token_usage
            if not isinstance(result.content, str) or not result.content.strip():
                raise RuntimeError('Provider returned empty or invalid content')
            attempt.content = result.content
            attempt.token_usage = result.token_usage
            attempt.status = InvocationAttemptStatus.SUCCEEDED
            return attempt
        except BaseException as exc:
            attempt.status = InvocationAttemptStatus.FAILED
            attempt.error = str(exc)
            session.status = InvocationSessionStatus.CANCELLED if isinstance(exc, asyncio.CancelledError) else InvocationSessionStatus.FAILED
            raise
        finally:
            if prompt_hooks is not None:
                evidence = {**session.metadata["anti_ai_prompt"],
                            "content_hash": hashlib.sha256(attempt.content.encode("utf-8")).hexdigest()}
                scan_error = None
                if attempt.status == InvocationAttemptStatus.SUCCEEDED:
                    try:
                        from application.ai_invocation.prose_execution_evidence import archive_prose_scan_receipt
                        evidence["anti_ai_scan"] = archive_prose_scan_receipt(
                            prompt_hooks.db,
                            novel_id=session.context.get("novel_id"),
                            chapter_number=session.context.get("chapter_number"),
                            attempt_id=attempt.id,
                            content=attempt.content,
                            scene_type=str(evidence.get("scene_type") or "default"),
                        )
                        attempt.accounting["anti_ai_scan"] = evidence["anti_ai_scan"]
                    except ProseExecutionEvidenceError as exc:
                        scan_error = exc
                        attempt.status = InvocationAttemptStatus.FAILED
                        attempt.error = str(exc)
                        session.status = InvocationSessionStatus.FAILED
                receipt = archive_prompt_use(prompt_hooks.db, evidence,
                    context=session.context, job_id=attempt.id,
                    status="completed" if attempt.status == InvocationAttemptStatus.SUCCEEDED else "cancelled" if session.status == InvocationSessionStatus.CANCELLED else "failed", error=attempt.error)
                attempt.accounting["anti_ai_prompt"] = receipt
                if scan_error:
                    raise scan_error
                if attempt.status == InvocationAttemptStatus.SUCCEEDED and receipt.get("persistence_status") != "committed":
                    attempt.status = InvocationAttemptStatus.FAILED
                    attempt.error = "去味提示词执行回执未持久化，正文不可采纳"
                    session.status = InvocationSessionStatus.FAILED
                    raise ProseExecutionEvidenceError(attempt.error)

    async def generate_streaming(
        self,
        *,
        session: InvocationSession,
        prompt_snapshot: PromptSnapshot,
        config: GenerationConfig | None = None,
        on_chunk: Callable[[str, str], None] | None = None,
        attempt: InvocationAttempt | None = None,
    ) -> InvocationAttempt:
        prompt_snapshot, prompt_hooks = self._prepare_prose_prompt(session, prompt_snapshot)
        if attempt is None:
            attempt = InvocationAttempt(
                id=str(uuid.uuid4()),
                session_id=session.id,
                status=InvocationAttemptStatus.RUNNING,
                prompt_snapshot=prompt_snapshot,
            )
        elif attempt.session_id != session.id or attempt.status != InvocationAttemptStatus.RUNNING:
            raise ValueError("Existing streaming attempt must be running in this session")
        else:
            attempt.prompt_snapshot = prompt_snapshot
        self._attempts[attempt.id] = attempt
        if attempt.id not in session.attempts:
            session.attempts.append(attempt.id)
        session.status = InvocationSessionStatus.GENERATING
        content_parts: list[str] = []
        stopped = False
        guard = None
        try:
            with capture_usage(novel_id=str(session.context.get('novel_id') or ''), attempt_id=attempt.id) as usage:
                stream = self._llm_service.stream_generate(prompt_snapshot.prompt, config or GenerationConfig())
                if is_prose_operation(session.operation):
                    guard = type(self).prose_stream_guard_factory(
                        novel_id=str(session.context.get("novel_id") or ""),
                        chapter_number=session.context.get("chapter_number"), job_id=attempt.id,
                        scene_type=session.metadata.get("anti_ai_prompt", {}).get("scene_type"),
                        prompt_evidence=session.metadata.get("anti_ai_prompt"),
                    )
                    stream = guard.wrap(stream)
                try:
                    async for chunk in stream:
                        if not chunk:
                            continue
                        content_parts.append(chunk)
                        attempt.content = "".join(content_parts)
                        if on_chunk is not None:
                            keep_going = on_chunk(chunk, attempt.content)
                            if keep_going is False:
                                stopped = True
                                break
                    if not stopped and not attempt.content.strip():
                        raise RuntimeError('Provider stream returned empty content')
                finally:
                    try:
                        if hasattr(stream, 'aclose'):
                            await stream.aclose()
                    finally:
                        attempt.token_usage = usage.token_usage if not stopped else None
                        attempt.accounting = dict(usage.metadata)
                        if guard is not None:
                            attempt.accounting["anti_ai_stream"] = dict(guard.summary)
                            if guard.summary.get("prompt_receipt"):
                                attempt.accounting["anti_ai_prompt"] = dict(guard.summary["prompt_receipt"])
            attempt.content = "".join(content_parts)
            if stopped:
                attempt.status = InvocationAttemptStatus.FAILED
                attempt.error = "streaming stopped"
                session.status = InvocationSessionStatus.CANCELLED
            else:
                attempt.status = InvocationAttemptStatus.SUCCEEDED
                if guard is not None:
                    stream_receipt = dict(guard.summary or {})
                    prompt_receipt = dict(stream_receipt.get("prompt_receipt") or {})
                    if (stream_receipt.get("persistence_status") != "committed"
                            or prompt_receipt.get("persistence_status") != "committed"):
                        attempt.status = InvocationAttemptStatus.FAILED
                        attempt.error = "去味流式执行回执未持久化，正文不可采纳"
                        session.status = InvocationSessionStatus.FAILED
                        raise ProseExecutionEvidenceError(attempt.error)
            return attempt
        except BaseException as exc:
            attempt.status = InvocationAttemptStatus.FAILED
            attempt.error = str(exc)
            session.status = InvocationSessionStatus.CANCELLED if isinstance(exc, asyncio.CancelledError) else InvocationSessionStatus.FAILED
            raise
        finally:
            # AntiAIStreamGuard owns the prompt receipt for streaming calls;
            # keep the generic receipt for non-stream generation only.
            if prompt_hooks is not None and guard is None:
                evidence = {**session.metadata["anti_ai_prompt"],
                            "content_hash": hashlib.sha256(attempt.content.encode("utf-8")).hexdigest()}
                receipt = archive_prompt_use(prompt_hooks.db, evidence,
                    context=session.context, job_id=attempt.id,
                    status="completed" if attempt.status == InvocationAttemptStatus.SUCCEEDED else "cancelled" if session.status == InvocationSessionStatus.CANCELLED else "failed", error=attempt.error)
                attempt.accounting["anti_ai_prompt"] = receipt
                if attempt.status == InvocationAttemptStatus.SUCCEEDED and receipt.get("persistence_status") != "committed":
                    attempt.status = InvocationAttemptStatus.FAILED
                    attempt.error = "去味提示词执行回执未持久化，正文不可采纳"
                    session.status = InvocationSessionStatus.FAILED
                    raise ProseExecutionEvidenceError(attempt.error)

    def get(self, attempt_id: str) -> InvocationAttempt:
        return self._attempts[attempt_id]


class AdoptionService:
    """形成采纳或拒绝决策，不执行提交副作用。"""

    def __init__(self):
        self._decisions: dict[str, AdoptionDecision] = {}

    def accept(
        self,
        *,
        session: InvocationSession,
        attempt: InvocationAttempt,
        accepted_by: str = "system",
        commit_prompt_version: bool = False,
        commit_variable_outputs: bool = False,
        commit_variable_bindings: bool = False,
        metadata: dict | None = None,
    ) -> AdoptionDecision:
        if attempt.session_id != session.id:
            raise ValueError("attempt 不属于当前 invocation session")
        if is_prose_operation(session.operation) and attempt.status != InvocationAttemptStatus.SUCCEEDED:
            # A generated body whose durable Anti-AI receipts failed is retained
            # for diagnostics, but can never be promoted into an adoption decision.
            accounting = dict(attempt.accounting or {})
            prompt_receipt = dict(accounting.get("anti_ai_prompt") or {})
            stream_receipt = dict(accounting.get("anti_ai_stream") or {})
            receipt_persistence_failed = any(
                receipt.get("persistence_status") == "failed" or receipt.get("archive_error")
                for receipt in (prompt_receipt, stream_receipt)
            )
            if receipt_persistence_failed:
                from application.ai_invocation.prose_execution_evidence import ProseExecutionEvidenceError

                raise ProseExecutionEvidenceError("去味执行回执未持久化，失败正文不可采纳")
        if attempt.status != InvocationAttemptStatus.SUCCEEDED:
            raise ValueError("只有成功的 attempt 可以被采纳")
        proof = None
        if is_prose_operation(session.operation):
            from infrastructure.persistence.database.connection import get_database
            proof = verify_attempt_prose_receipts(get_database(), session=session, attempt=attempt)
        decision = AdoptionDecision(
            id=str(uuid.uuid4()),
            session_id=session.id,
            attempt_id=attempt.id,
            accept_content=True,
            commit_prompt_version=commit_prompt_version,
            commit_variable_outputs=commit_variable_outputs,
            commit_variable_bindings=commit_variable_bindings,
            accepted_content=attempt.content,
            accepted_by=accepted_by,
            metadata={**dict(metadata or {}), **({"_prose_execution_evidence": proof} if proof else {})},
        )
        self._decisions[decision.id] = decision
        session.status = InvocationSessionStatus.AWAITING_COMMIT
        return decision

    def reject(self, *, session: InvocationSession, attempt: InvocationAttempt, accepted_by: str = "system") -> AdoptionDecision:
        if attempt.session_id != session.id:
            raise ValueError("attempt 不属于当前 invocation session")
        decision = AdoptionDecision(
            id=str(uuid.uuid4()),
            session_id=session.id,
            attempt_id=attempt.id,
            decision="rejected",
            accept_content=False,
            accepted_content="",
            accepted_by=accepted_by,
        )
        self._decisions[decision.id] = decision
        session.status = InvocationSessionStatus.AWAITING_PRE_CALL_REVIEW
        return decision

    def get(self, decision_id: str) -> AdoptionDecision:
        return self._decisions[decision_id]


class AdoptionCommitService:
    """幂等提交采纳结果。

    当前只实现最小内容提交步骤；CPMS 版本、变量绑定、变量输出和 continuation
    后续通过独立 step 扩展，不能再绕回 Gateway 或业务层硬编码。
    """

    def __init__(self, prompt_manager=None, variable_hub_repository: VariableHubRepository | None = None):
        self._commits_by_key: dict[str, AdoptionCommit] = {}
        self._prompt_manager = prompt_manager
        self._variable_hub_repository = variable_hub_repository

    def _get_prompt_manager(self):
        if self._prompt_manager is None:
            from infrastructure.ai.prompt_manager import get_prompt_manager

            self._prompt_manager = get_prompt_manager()
        return self._prompt_manager

    def _get_variable_hub_repository(self):
        if self._variable_hub_repository is None:
            try:
                from infrastructure.persistence.database.connection import get_database
                from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository

                self._variable_hub_repository = SqliteVariableHubRepository(get_database())
            except Exception:
                self._variable_hub_repository = None
        return self._variable_hub_repository

    def _commit_prompt_version(self, *, session: InvocationSession, decision: AdoptionDecision) -> dict:
        snapshot = session.prompt_snapshot
        if snapshot is None:
            return {"skipped": True, "reason": "missing_prompt_snapshot"}
        if snapshot.draft_prompt is None:
            return {"skipped": True, "reason": "missing_draft_prompt"}
        if snapshot.template_prompt is not None and snapshot.draft_prompt == snapshot.template_prompt:
            return {"skipped": True, "reason": "draft_unchanged"}

        mgr = self._get_prompt_manager()
        mgr.ensure_seeded()
        node = mgr.get_node(snapshot.node_key or session.node_key, by_key=True)
        if node is None:
            raise ValueError(f"CPMS 节点不存在，无法写回提示词版本: {snapshot.node_key or session.node_key}")

        previous_version_id = node.active_version_id or ""
        updated = mgr.update_node(
            node.id,
            system_prompt=snapshot.draft_prompt.system,
            user_template=snapshot.draft_prompt.user,
            change_summary=f"AI Invocation 采纳写回: {session.operation}",
        )
        if updated is None:
            raise ValueError(f"CPMS 节点更新失败: {snapshot.node_key or session.node_key}")

        updated_system = (
            getattr(updated, "get_active_system", lambda: "")()
            or snapshot.draft_prompt.system
        )
        updated_user = (
            getattr(updated, "get_active_user_template", lambda: "")()
            or snapshot.draft_prompt.user
        )
        updated_version_id = getattr(updated, "active_version_id", None) or previous_version_id
        refreshed_template = Prompt(system=updated_system, user=updated_user)
        rendered_prompt = refreshed_template
        if session.variable_plan is not None:
            from infrastructure.ai.prompt_template_engine import get_template_engine

            render_result = get_template_engine().render(
                system_template=updated_system,
                user_template=updated_user,
                variables=dict(session.variable_plan.aliases or {}),
            )
            rendered_prompt = Prompt(
                system=render_result.system or "",
                user=render_result.user or "",
            )

        try:
            from infrastructure.ai.prompt_registry import get_prompt_registry

            get_prompt_registry().invalidate_cache(updated.node_key)
        except Exception:
            logger.warning(
                "failed to invalidate prompt registry cache after cpms commit: node=%s",
                updated.node_key,
                exc_info=True,
            )

        try:
            from infrastructure.persistence.database.connection import get_database
            from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
                SqliteInvocationSpecRepository,
            )

            spec_repo = SqliteInvocationSpecRepository(get_database())
            current_spec = spec_repo.get(session.operation, session.node_key)
            if current_spec is not None:
                spec_repo.upsert(
                    replace(
                        current_spec,
                        prompt_node_version_id=updated_version_id,
                    )
                )
        except Exception:
            logger.warning(
                "failed to refresh invocation spec after cpms commit: operation=%s node=%s",
                session.operation,
                session.node_key,
                exc_info=True,
            )

        session.prompt_snapshot = replace(
            snapshot,
            prompt=rendered_prompt,
            node_version_id=updated_version_id,
            template_prompt=refreshed_template,
            draft_prompt=refreshed_template,
            template_hash=stable_hash(
                {
                    "system_template": refreshed_template.system,
                    "user_template": refreshed_template.user,
                }
            ),
            composition_hash=stable_hash(
                {
                    "node_key": session.node_key,
                    "node_version_id": updated_version_id,
                    "asset_link_set_id": snapshot.asset_link_set_id,
                    "input_binding_set_id": snapshot.input_binding_set_id,
                    "output_binding_set_id": snapshot.output_binding_set_id,
                    "asset_version_ids": tuple(snapshot.asset_version_ids or ()),
                }
            ),
            rendered_prompt_hash=prompt_hash(rendered_prompt),
        )
        logger.info(
            "refreshed invocation prompt snapshot after cpms commit: session=%s node=%s version=%s",
            session.id,
            updated.node_key,
            updated.active_version_id,
        )

        return {
            "skipped": False,
            "node_key": updated.node_key,
            "node_id": updated.id,
            "previous_version_id": previous_version_id,
            "active_version_id": updated_version_id,
            "template_hash": stable_hash(
                {
                    "system_template": snapshot.draft_prompt.system,
                    "user_template": snapshot.draft_prompt.user,
                }
            ),
            "accepted_by": decision.accepted_by,
        }

    def _materialize_output_value(self, alias: str, value: Any) -> Any:
        if isinstance(value, Mapping) and alias in value and len(value) == 1:
            return value[alias]
        return value

    def _commit_variable_outputs(
        self,
        *,
        session: InvocationSession,
        decision: AdoptionDecision,
        commit_id: str,
        output_payload: Mapping[str, Any] | None = None,
    ) -> dict:
        snapshot = session.prompt_snapshot
        if snapshot is None:
            return {"skipped": True, "reason": "missing_prompt_snapshot"}
        repo = self._get_variable_hub_repository()
        if repo is None:
            return {"skipped": True, "reason": "missing_variable_hub_repository"}

        if session.operation.startswith("bible.setup."):
            try:
                from application.world.services.bible_setup_output_bindings import ensure_bible_setup_output_bindings

                ensure_bible_setup_output_bindings(repo, session.node_key)
            except Exception as exc:
                logger.warning(
                    "Failed to ensure Bible setup output bindings: session=%s node=%s error=%s",
                    session.id,
                    session.node_key,
                    exc,
                )

        bindings = []
        if snapshot.output_binding_set_id:
            try:
                bindings = repo.get_output_bindings(snapshot.output_binding_set_id, snapshot.node_key)
            except Exception as exc:
                return {"skipped": True, "reason": "output_bindings_unavailable", "error": str(exc)}

        if not bindings:
            return {"skipped": True, "reason": "no_output_bindings"}

        required_aliases = [
            binding.alias
            for binding in bindings
            if binding.enabled and binding.variable_key and binding.required
        ]

        payload = dict(output_payload or {})
        payload_from_decision = False
        if not payload:
            try:
                parsed = json.loads(decision.accepted_content)
            except Exception:
                parsed = None
            if parsed is None and required_aliases:
                return {
                    "blocked": True,
                    "reason": "accepted_content_not_json_object",
                    "required_aliases": required_aliases,
                }
            payload = dict(parsed) if isinstance(parsed, Mapping) else {}
            payload_from_decision = True

        if not payload and required_aliases:
            return {
                "blocked": True,
                "reason": "missing_required_output_payload",
                "required_aliases": required_aliases,
                "payload_source": "decision" if payload_from_decision else "continuation",
            }

        written: list[dict[str, Any]] = []
        missing_required: list[str] = []
        for binding in bindings:
            if not binding.enabled or not binding.variable_key:
                continue
            raw_value = resolve_output_payload_value(
                payload,
                binding.source_path or binding.alias,
                binding.alias if binding.source_path else "",
                binding.variable_key,
            )
            if raw_value is None:
                if binding.required:
                    missing_required.append(binding.alias)
                continue
            write = VariableWrite(
                key=binding.variable_key,
                value=self._materialize_output_value(binding.alias, raw_value),
                context_key=self._context_key(session.context, binding.scope),
                source_session_id=session.id,
                source_attempt_id=decision.attempt_id,
                source_trace_id=str(session.metadata.get("trace_id") or session.id),
                source_node_key=session.node_key,
                source_commit_id=commit_id,
                lineage={
                    "alias": binding.alias,
                    "binding_set_id": snapshot.output_binding_set_id,
                    "operation": session.operation,
                    "source_path": binding.source_path or binding.alias,
                },
                value_type=binding.value_type,
                display_name=binding.display_name,
                scope=binding.scope,
                stage=binding.stage,
            )
            stored = repo.set_value(write)
            written.append(
                {
                    "alias": binding.alias,
                    "variable_key": binding.variable_key,
                    "context_key": write.context_key,
                    "version_number": getattr(stored, "version_number", 1),
                }
            )
        if missing_required:
            return {
                "blocked": True,
                "reason": "missing_required_output_aliases",
                "missing_aliases": missing_required,
                "binding_set_id": snapshot.output_binding_set_id,
            }
        if not written:
            return {"skipped": True, "reason": "no_matching_output_values"}
        return {
            "skipped": False,
            "written": written,
            "binding_set_id": snapshot.output_binding_set_id,
        }

    def _commit_projection(self, *, session: InvocationSession, continuation_result: Mapping[str, Any] | None) -> dict:
        projection = dict((continuation_result or {}).get("_projection") or {})
        if not projection:
            return {"skipped": True, "reason": "no_projection_plan"}
        adapter = str(projection.get("adapter") or "").strip()
        if adapter != "chapters_table":
            return {"blocked": True, "reason": "unsupported_projection_adapter", "adapter": adapter}
        try:
            from infrastructure.persistence.database.connection import get_database
            from application.ai_invocation.contracts.chapter_prose_generation import project_chapter_prose_to_chapters

            return project_chapter_prose_to_chapters(get_database(), projection)
        except ProseExecutionEvidenceError:
            # Integrity failures must reach the API/DAG boundary.  Treating
            # them as a generic projection error would make a tampered prose
            # result look like an ordinary retryable write failure.
            raise
        except Exception as exc:
            return {"blocked": True, "reason": "projection_failed", "error": str(exc)}

    @staticmethod
    def _context_key(context: Mapping[str, Any], scope: str = "") -> str:
        novel_id = str(context.get("novel_id") or "").strip()
        chapter_number = context.get("chapter_number")
        beat_index = context.get("beat_index")
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope == "beat" and novel_id and chapter_number not in (None, "") and beat_index not in (None, ""):
            return f"novel_id:{novel_id}|chapter_number:{chapter_number}|beat_index:{beat_index}"
        if normalized_scope in {"beat", "chapter", "scene"} and novel_id and chapter_number not in (None, ""):
            return f"novel_id:{novel_id}|chapter_number:{chapter_number}"
        if novel_id:
            return f"novel_id:{novel_id}"
        return "global"

    def commit(self, *, session: InvocationSession, decision: AdoptionDecision) -> AdoptionCommit:
        if decision.session_id != session.id:
            raise ValueError("decision 不属于当前 invocation session")
        key = f"{session.id}:{decision.id}"
        existing = self._commits_by_key.get(key)
        if existing is not None:
            return existing
        if decision.decision != "accepted" or not decision.accept_content:
            session.status = InvocationSessionStatus.CANCELLED
            commit = AdoptionCommit(
                id=str(uuid.uuid4()),
                session_id=session.id,
                decision_id=decision.id,
                status=AdoptionCommitStatus.SUCCEEDED,
                steps=[
                    AdoptionCommitStep(
                        name="commit_content_patch",
                        status=AdoptionCommitStatus.SUCCEEDED,
                        result={"skipped": True, "reason": "decision_not_accepted"},
                    )
                ],
            )
            self._commits_by_key[key] = commit
            return commit

        if is_prose_operation(session.operation):
            from infrastructure.persistence.database.connection import get_database
            # A receipt can be deleted or altered after /accept. Re-read it at
            # the commit boundary before any variable or chapter projection.
            try:
                verify_decision_prose_receipts(
                    get_database(), session=session, decision=decision,
                    proof=dict((decision.metadata or {}).get("_prose_execution_evidence") or {}),
                )
            except ProseExecutionEvidenceError:
                session.status = InvocationSessionStatus.BLOCKED
                raise

        session.status = InvocationSessionStatus.COMMITTING
        commit = AdoptionCommit(
            id=str(uuid.uuid4()),
            session_id=session.id,
            decision_id=decision.id,
            status=AdoptionCommitStatus.RUNNING,
        )
        try:
            commit.steps.append(
                AdoptionCommitStep(
                    name="commit_content_patch",
                    status=AdoptionCommitStatus.SUCCEEDED,
                    result={
                        "content_hash": stable_hash({"content": decision.accepted_content}),
                        "content_length": len(decision.accepted_content),
                    },
                )
            )
            prompt_version_result = self._commit_prompt_version(session=session, decision=decision)
            commit.steps.append(
                AdoptionCommitStep(
                    name="commit_prompt_version",
                    status=AdoptionCommitStatus.SUCCEEDED,
                    result=prompt_version_result,
                )
            )
            if not prompt_version_result.get("skipped"):
                commit.result = {**commit.result, "prompt_version": prompt_version_result}
            try:
                continuation_result = execute_continuation(ContinuationContext(session=session, decision=decision))
            except Exception as exc:
                commit.steps.append(
                    AdoptionCommitStep(
                        name="continuation_handler",
                        status=AdoptionCommitStatus.FAILED,
                        result={
                            "error": str(exc),
                            "accepted_content_preview": (decision.accepted_content or "")[:1200],
                        },
                        error=str(exc),
                    )
                )
                commit.status = AdoptionCommitStatus.FAILED
                commit.error = str(exc)
                commit.result = {
                    **commit.result,
                    "accepted_content": decision.accepted_content,
                    "continuation_error": str(exc),
                }
                session.status = InvocationSessionStatus.BLOCKED
                return commit
            if continuation_result:
                commit.steps.append(
                    AdoptionCommitStep(
                        name="continuation_handler",
                        status=AdoptionCommitStatus.SUCCEEDED,
                        result=continuation_result,
                    )
                )
                commit.result = {**commit.result, "continuation": continuation_result}
            variable_output_result = self._commit_variable_outputs(
                session=session,
                decision=decision,
                commit_id=commit.id,
                output_payload=continuation_result,
            )
            output_step_status = (
                AdoptionCommitStatus.BLOCKED
                if variable_output_result.get("blocked")
                else AdoptionCommitStatus.SUCCEEDED
            )
            commit.steps.append(
                AdoptionCommitStep(
                    name="commit_variable_outputs",
                    status=output_step_status,
                    result=variable_output_result,
                )
            )
            if variable_output_result.get("blocked"):
                commit.status = AdoptionCommitStatus.BLOCKED
                commit.error = str(variable_output_result.get("reason") or "required_output_missing")
                commit.result = {
                    **commit.result,
                    "accepted_content": decision.accepted_content,
                    "variable_outputs": variable_output_result,
                }
                session.status = InvocationSessionStatus.BLOCKED
                return commit
            if not variable_output_result.get("skipped"):
                commit.result = {**commit.result, "variable_outputs": variable_output_result}
            projection_result = self._commit_projection(session=session, continuation_result=continuation_result)
            projection_step_status = (
                AdoptionCommitStatus.BLOCKED
                if projection_result.get("blocked")
                else AdoptionCommitStatus.SUCCEEDED
            )
            commit.steps.append(
                AdoptionCommitStep(
                    name="commit_projection",
                    status=projection_step_status,
                    result=projection_result,
                )
            )
            if projection_result.get("blocked"):
                commit.status = AdoptionCommitStatus.BLOCKED
                commit.error = str(projection_result.get("reason") or "projection_failed")
                commit.result = {
                    **commit.result,
                    "accepted_content": decision.accepted_content,
                    "projection": projection_result,
                }
                session.status = InvocationSessionStatus.BLOCKED
                return commit
            if not projection_result.get("skipped"):
                commit.result = {**commit.result, "projection": projection_result}
            commit.status = AdoptionCommitStatus.SUCCEEDED
            commit.result = {**commit.result, "accepted_content": decision.accepted_content}
            session.status = InvocationSessionStatus.COMPLETED
        except Exception as exc:
            commit.status = AdoptionCommitStatus.FAILED
            commit.error = str(exc)
            session.status = InvocationSessionStatus.FAILED
            raise
        finally:
            self._commits_by_key[key] = commit
        return commit
