from __future__ import annotations

import time

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.core.v1_length_tiers import build_v1_structure_black_box_hint
from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue
from application.ai_invocation.variable_hub import VariableWrite
from interfaces.api.v1.engine import ai_invocation_routes


class _StreamingLLM:
    async def generate(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        return GenerationResult(
            content="HTTP正文",
            token_usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def stream_generate(self, prompt: Prompt, config: GenerationConfig):
        yield "HTTP"
        yield "正文"


def _wait_for_status(client: TestClient, session_id: str, expected: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        latest = client.get(f"/ai-invocations/{session_id}").json()
        if latest["session"]["status"] == expected:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"session {session_id} did not reach {expected}: {latest}")


def test_chapter_prose_invocation_http_lifecycle_writes_variable_hub_and_chapters(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test.db"))

    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
                ("novel-http", "HTTP小说", "novel-http", 12),
            )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)

    create_payload = {
        "operation": "chapter.generate.prose",
        "node_key": "chapter-prose-generation",
        "policy": "FULL_INTERACTIVE",
        "context": {"novel_id": "novel-http", "chapter_number": 6},
        "variables": {
            # This unbound request alias cannot replace the saved novel fact.
            "novel_title": "请求中的旧书名",
            "chapter_number": 6,
            "chapter_outline": "从审阅面板生成正文",
        },
    }
    created = client.post("/ai-invocations", json=create_payload)
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]
    assert created.json()["session"]["status"] == "awaiting_pre_call_review"

    input_repo = SqliteVariableHubRepository(db)
    assert input_repo.get_value("novel.setup.title", "novel_id:novel-http").value == "HTTP小说"
    assert input_repo.get_value("chapter.outline", "novel_id:novel-http|chapter_number:6").value == "从审阅面板生成正文"

    resumed = client.post(f"/ai-invocations/{session_id}/resume", json={"resumed_by": "test"})
    assert resumed.status_code == 200, resumed.text
    accepted_ready = _wait_for_status(client, session_id, "awaiting_acceptance")
    attempt_id = accepted_ready["attempt"]["id"]
    assert accepted_ready["attempt"]["content"] == "HTTP正文"

    accepted = client.post(
        f"/ai-invocations/{session_id}/accept",
        json={"attempt_id": attempt_id, "accepted_by": "test"},
    )
    assert accepted.status_code == 200, accepted.text
    decision_id = accepted.json()["decision"]["id"]

    committed = client.post(f"/ai-invocations/{session_id}/commits", json={"decision_id": decision_id})
    assert committed.status_code == 200, committed.text
    assert committed.json()["session"]["status"] == "completed"
    assert committed.json()["commit"]["status"] == "succeeded"

    output_repo = SqliteVariableHubRepository(db)
    assert output_repo.get_value("chapter.prose.generated", "novel_id:novel-http|chapter_number:6").value == "HTTP正文"
    assert output_repo.get_value("chapter.prose.accepted", "novel_id:novel-http|chapter_number:6").value == "HTTP正文"
    row = db.fetch_one(
        "SELECT content, status, word_count FROM chapters WHERE novel_id = ? AND number = ?",
        ("novel-http", 6),
    )
    assert row == {"content": "HTTP正文", "status": "draft", "word_count": 6}

    db.close_all(skip_checkpoint=True)


def test_output_bindings_expose_only_user_defined_target_display_names(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test-output-binding-names.db"))

    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
                ("novel-output-names", "输出命名小说", "novel-output-names", 12),
            )

    variable_repo = SqliteVariableHubRepository(db)
    variable_repo.set_value(
        VariableWrite(
            key="chapter.prose.generated",
            value="历史正文",
            context_key="novel_id:novel-output-names|chapter_number:1",
            source_node_key="test",
            value_type="string",
            display_name="正文入库名称",
            scope="chapter",
            stage="writing",
        )
    )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)

    created = client.post(
        "/ai-invocations",
        json={
            "operation": "chapter.generate.prose",
            "node_key": "chapter-prose-generation",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-output-names", "chapter_number": 1},
            "variables": {
                "novel_title": "输出命名小说",
                "chapter_number": 1,
                "chapter_outline": "检查输出绑定展示名称",
            },
        },
    )
    assert created.status_code == 200, created.text

    bindings = created.json()["session"]["output_bindings"]
    generated = next(item for item in bindings if item["variable_key"] == "chapter.prose.generated")
    accepted = next(item for item in bindings if item["variable_key"] == "chapter.prose.accepted")

    assert generated["display_name"] == "生成正文"
    assert generated["target_display_name"] == "正文入库名称"
    assert accepted["display_name"] == "采纳正文"
    assert accepted["target_display_name"] == ""

    db.close_all(skip_checkpoint=True)


def test_bible_setup_invocation_materializes_inputs_and_get_refreshes_snapshot(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test-bible-vars.db"))

    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)
    internal_hint = build_v1_structure_black_box_hint("standard", 500, 2000)
    author_premise = "旧设定：少年在海底城觉醒"

    created = client.post(
        "/ai-invocations",
        json={
            "operation": "bible.setup.worldbuilding",
            "node_key": "bible-worldbuilding",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-bible-vars"},
            "variables": {
                "novel_title": "变量小说",
                "premise": f"{internal_hint}\n\n{author_premise}",
                "target_chapters": 120,
                "target_words_per_chapter": 2500,
                "genre_opening_profile": {"source_level": "test", "opening_mechanism": "压迫开局"},
                "genre_reader_contract": {"reader_promise": "升级破局"},
                "genre_rhythm_constraints": {"payoff_interval": "三章一回收"},
            },
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]

    repo = SqliteVariableHubRepository(db)
    assert repo.get_value("novel.setup.premise", "novel_id:novel-bible-vars").value == author_premise
    assert repo.get_value("novel.setup.summary", "novel_id:novel-bible-vars") is None
    assert repo.get_value("system.worldbuilding.fields_desc", "novel_id:novel-bible-vars") is None
    assert repo.get_value("novel.worldbuilding.full", "novel_id:novel-bible-vars") is None
    assert repo.get_value("novel.genre.opening_profile", "novel_id:novel-bible-vars") is None
    assert repo.get_value("novel.genre.reader_contract", "novel_id:novel-bible-vars") is None

    repo.set_value(
        VariableWrite(
            key="novel.setup.premise",
            value="新设定：变量中心改为天空城债务法则",
            context_key="novel_id:novel-bible-vars",
            source_node_key="test",
        )
    )
    refreshed = client.get(f"/ai-invocations/{session_id}")
    assert refreshed.status_code == 200, refreshed.text
    plan = refreshed.json()["session"]["variable_plan"]
    assert plan["aliases"]["premise"] == "新设定：变量中心改为天空城债务法则"
    assert plan["aliases"]["genre_reader_contract"]["reader_promise"] == "升级破局"
    assert any(
        item["key"] == "premise" and item["value"] == "新设定：变量中心改为天空城债务法则"
        for item in plan["snapshot_items"]
    )
    assert all(item["key"] != "fields_desc" for item in plan["snapshot_items"])
    assert all(item["key"] != "genre_reader_contract" for item in plan["snapshot_items"])
    assert "新设定：变量中心改为天空城债务法则" in refreshed.json()["session"]["prompt_snapshot"]["prompt"]["user"]

    db.close_all(skip_checkpoint=True)


def test_chapter_prose_prompt_draft_custom_variable_can_be_filled_and_resumed(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test-custom-var.db"))

    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
                ("novel-custom", "变量小说", "novel-custom", 12),
            )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)

    created = client.post(
        "/ai-invocations",
        json={
            "operation": "chapter.generate.prose",
            "node_key": "chapter-prose-generation",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-custom", "chapter_number": 3},
            "variables": {
                "novel_title": "变量小说",
                "chapter_number": 3,
                "chapter_outline": "补齐自定义变量后生成正文",
            },
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]
    template = created.json()["session"]["prompt_snapshot"]["template_prompt"]

    saved = client.put(
        f"/ai-invocations/{session_id}/prompt-draft",
        json={
            "system_template": template["system"],
            "user_template": template["user"] + "\n特殊要求：{{chapter.special_requirement}}",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["session"]["status"] == "blocked"
    assert "chapter_special_requirement" in saved.json()["session"]["variable_plan"]["required_missing"]

    updated = client.put(
        f"/ai-invocations/{session_id}/variables",
        json={"values": {"chapter_special_requirement": "雨夜压迫感"}, "updated_by": "test"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["session"]["status"] == "awaiting_pre_call_review"
    assert updated.json()["session"]["variable_plan"]["required_missing"] == []

    variable_repo = SqliteVariableHubRepository(db)
    stored = variable_repo.get_value(
        "chapter.special_requirement",
        "novel_id:novel-custom|chapter_number:3",
    )
    assert stored is not None and stored.value == "雨夜压迫感"

    resumed = client.post(f"/ai-invocations/{session_id}/resume", json={"resumed_by": "test"})
    assert resumed.status_code == 200, resumed.text
    accepted_ready = _wait_for_status(client, session_id, "awaiting_acceptance")
    assert accepted_ready["attempt"]["content"] == "HTTP正文"

    db.close_all(skip_checkpoint=True)


def test_chapter_prose_prompt_draft_blank_templates_return_400(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test-blank-prompt-draft.db"))

    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
                ("novel-blank-draft", "空草稿小说", "novel-blank-draft", 12),
            )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)

    created = client.post(
        "/ai-invocations",
        json={
            "operation": "chapter.generate.prose",
            "node_key": "chapter-prose-generation",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-blank-draft", "chapter_number": 4},
            "variables": {
                "novel_title": "空草稿小说",
                "chapter_number": 4,
                "chapter_outline": "验证空 prompt 草稿校验",
            },
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]

    preview = client.post(
        f"/ai-invocations/{session_id}/prompt-draft/preview",
        json={"system_template": "", "user_template": ""},
    )
    assert preview.status_code == 400, preview.text
    assert preview.json()["detail"] == "User message cannot be empty"

    saved = client.put(
        f"/ai-invocations/{session_id}/prompt-draft",
        json={"system_template": "", "user_template": ""},
    )
    assert saved.status_code == 400, saved.text
    assert saved.json()["detail"] == "User message cannot be empty"

    db.close_all(skip_checkpoint=True)


def test_chapter_prose_commit_can_promote_prompt_draft_to_cpms(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test-prose-commit-prompt.db"))

    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
                ("novel-commit-prompt", "提示词小说", "novel-commit-prompt", 12),
            )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)

    manager = prompt_manager_module._manager_instance
    before_node = manager.get_node("chapter-prose-generation", by_key=True)
    assert before_node is not None
    before_version_id = before_node.active_version_id
    before_user_template = before_node.get_active_user_template()

    created = client.post(
        "/ai-invocations",
        json={
            "operation": "chapter.generate.prose",
            "node_key": "chapter-prose-generation",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-commit-prompt", "chapter_number": 2},
            "variables": {
                "novel_title": "提示词小说",
                "chapter_number": 2,
                "chapter_outline": "提交后应覆写 CPMS 提示词",
            },
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]
    template = created.json()["session"]["prompt_snapshot"]["template_prompt"]

    suffix = "\n额外要求：生成时增强临场感与压迫感。"
    saved = client.put(
        f"/ai-invocations/{session_id}/prompt-draft",
        json={
            "system_template": template["system"],
            "user_template": template["user"] + suffix,
        },
    )
    assert saved.status_code == 200, saved.text

    resumed = client.post(f"/ai-invocations/{session_id}/resume", json={"resumed_by": "test"})
    assert resumed.status_code == 200, resumed.text
    accepted_ready = _wait_for_status(client, session_id, "awaiting_acceptance")
    attempt_id = accepted_ready["attempt"]["id"]

    accepted = client.post(
        f"/ai-invocations/{session_id}/accept",
        json={
            "attempt_id": attempt_id,
            "accepted_by": "test",
            "commit_prompt_version": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["decision"]["commit_prompt_version"] is True
    decision_id = accepted.json()["decision"]["id"]

    committed = client.post(f"/ai-invocations/{session_id}/commits", json={"decision_id": decision_id})
    assert committed.status_code == 200, committed.text
    steps = committed.json()["commit"]["steps"]
    prompt_step = next(step for step in steps if step["name"] == "commit_prompt_version")
    assert prompt_step["status"] == "succeeded"
    assert prompt_step["result"]["skipped"] is False

    after_node = manager.get_node("chapter-prose-generation", by_key=True)
    assert after_node is not None
    assert after_node.active_version_id != before_version_id
    assert after_node.get_active_user_template() == before_user_template + suffix

    created_again = client.post(
        "/ai-invocations",
        json={
            "operation": "chapter.generate.prose",
            "node_key": "chapter-prose-generation",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-commit-prompt", "chapter_number": 3},
            "variables": {
                "novel_title": "提示词小说",
                "chapter_number": 3,
                "chapter_outline": "下一章应读取 CPMS 新提示词",
            },
        },
    )
    assert created_again.status_code == 200, created_again.text
    next_template = created_again.json()["session"]["prompt_snapshot"]["template_prompt"]["user"]
    assert next_template == before_user_template + suffix

    db.close_all(skip_checkpoint=True)


def test_chapter_prose_get_refresh_keeps_setup_snapshot_without_prompt_injection(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test-prose-snapshot.db"))

    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
                ("novel-snapshot", "快照小说", "novel-snapshot", 12),
            )

    variable_repo = SqliteVariableHubRepository(db)
    variable_repo.set_value(
        VariableWrite(
            key="novel.characters.list",
            value=[{"name": "变量角色"}],
            context_key="novel_id:novel-snapshot",
            source_node_key="test",
            value_type="list",
            display_name="角色列表",
            scope="novel",
            stage="characters",
        )
    )
    variable_repo.set_value(
        VariableWrite(
            key="novel.worldbuilding.core_rules",
            value={"power_system": "变量武道"},
            context_key="novel_id:novel-snapshot",
            source_node_key="test",
            value_type="object",
            display_name="核心法则",
            scope="novel",
            stage="worldbuilding",
        )
    )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)

    created = client.post(
        "/ai-invocations",
        json={
            "operation": "chapter.generate.prose",
            "node_key": "chapter-prose-generation",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-snapshot", "chapter_number": 1},
            "variables": {
                "novel_title": "快照小说",
                "chapter_number": 1,
                "chapter_outline": "生成正文",
            },
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]

    refreshed = client.get(f"/ai-invocations/{session_id}")
    assert refreshed.status_code == 200, refreshed.text
    plan = refreshed.json()["session"]["variable_plan"]
    keys = {item["variable_key"] for item in plan["snapshot_items"]}
    group_ids = {group["id"] for group in plan["snapshot_groups"]}

    assert "novel.characters.list" in keys
    assert "novel.worldbuilding.core_rules" in keys
    assert "novel:characters" in group_ids
    assert "novel:worldbuilding" in group_ids
    prompt_user = refreshed.json()["session"]["prompt_snapshot"]["prompt"]["user"]
    assert "变量角色" not in prompt_user
    assert "变量武道" not in prompt_user

    db.close_all(skip_checkpoint=True)


@pytest.fixture
def prose_http(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "prose-anti-ai-http.db"))
    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda db_path=None: db)

    class Provider(_StreamingLLM):
        def __init__(self):
            self.prompts = []

        async def stream_generate(self, prompt, config):
            self.prompts.append(prompt)
            yield "HTTP"
            yield "正文"

    provider = Provider()
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: provider)
    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module
    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute("INSERT INTO novels (id,title,slug,target_chapters) VALUES ('review-novel','审阅小说','review-novel',12)")
            conn.execute("""INSERT INTO unified_characters
                (id,novel_id,name,verbal_tic,created_at,updated_at)
                VALUES ('review-character','review-novel','林青','先等等',datetime('now'),datetime('now'))""")
    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    with TestClient(app) as client:
        created = client.post("/ai-invocations", json={
            "operation": "chapter.generate.prose",
            "node_key": "chapter-prose-generation",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "review-novel", "chapter_number": 1},
            "variables": {"novel_title": "审阅小说", "chapter_number": 1, "chapter_outline": "林青推门走入雨夜"},
        })
        assert created.status_code == 200, created.text
        yield db, client, provider, created.json()["session"]["id"]
    db.close_all(skip_checkpoint=True)


@pytest.mark.parametrize("endpoint", ["resume", "retry"])
@pytest.mark.parametrize("change", ["verbal_tic", "scene_type"])
def test_reviewed_prose_changes_require_review_before_provider_call(prose_http, endpoint, change):
    from application.ai_invocation.dtos import prompt_hash
    from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
    db, client, provider, session_id = prose_http
    if endpoint == "retry":
        assert client.post(f"/ai-invocations/{session_id}/resume", json={}).status_code == 200
        _wait_for_status(client, session_id, "awaiting_acceptance")
    calls_before = len(provider.prompts)
    if change == "verbal_tic":
        with sqlite_writes_bypass_queue():
            with db.transaction() as conn:
                conn.execute("UPDATE unified_characters SET verbal_tic='给我三息' WHERE id='review-character'")
    else:
        ChapterRewriteCandidates(db).set_policy("review-novel", "scan_only", scene_type="battle")

    response = client.post(f"/ai-invocations/{session_id}/{endpoint}", json={})
    assert response.status_code == 409, response.text
    assert len(provider.prompts) == calls_before
    refreshed = client.get(f"/ai-invocations/{session_id}").json()
    assert refreshed["session"]["status"] == "awaiting_pre_call_review"
    assert len(refreshed["session"]["attempts"]) == calls_before
    if change == "verbal_tic":
        assert "给我三息" in refreshed["session"]["prompt_snapshot"]["prompt"]["system"]
    else:
        assert refreshed["session"]["metadata"]["anti_ai_prompt"]["scene_type"] == "battle"

    assert client.post(f"/ai-invocations/{session_id}/resume", json={}).status_code == 200
    completed = _wait_for_status(client, session_id, "awaiting_acceptance")
    attempt = completed["attempt"]
    assert len(provider.prompts) == calls_before + 1
    assert len(completed["session"]["attempts"]) == calls_before + 1
    stream = db.fetch_one("SELECT * FROM anti_ai_stream_runs WHERE job_id=?", (attempt["id"],))
    prompt = db.fetch_one("SELECT * FROM anti_ai_prompt_runs WHERE job_id=?", (attempt["id"],))
    assert stream["status"] == stream["scan_status"] == "completed"
    assert prompt["status"] == "completed" and prompt["prompt_hash"] == prompt_hash(provider.prompts[-1])
    assert attempt["accounting"]["anti_ai_stream"]["run_id"] == stream["run_id"]
    assert attempt["accounting"]["anti_ai_prompt"]["run_id"] == prompt["run_id"]
    assert db.fetch_one("SELECT COUNT(*) AS total FROM anti_ai_prompt_runs WHERE job_id=?", (attempt["id"],))["total"] == 1


def test_http_prose_archive_failure_preserves_generated_text_with_failed_receipts(prose_http):
    db, client, _provider, session_id = prose_http
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute("DROP TABLE anti_ai_stream_runs")
            conn.execute("DROP TABLE anti_ai_prompt_runs")
    assert client.post(f"/ai-invocations/{session_id}/resume", json={}).status_code == 200
    failed = _wait_for_status(client, session_id, "failed")
    assert failed["attempt"]["status"] == "failed"
    assert failed["attempt"]["content"] == "HTTP正文"
    accounting = failed["attempt"]["accounting"]
    assert accounting["anti_ai_stream"]["persistence_status"] == "failed"
    assert accounting["anti_ai_prompt"]["persistence_status"] == "failed"

    rejected = client.post(
        f"/ai-invocations/{session_id}/accept",
        json={"attempt_id": failed["attempt"]["id"], "accepted_by": "test"},
    )
    assert rejected.status_code == 409, rejected.text
    blocked = client.get(f"/ai-invocations/{session_id}").json()
    assert blocked["session"]["status"] == "blocked"
    assert db.fetch_one(
        "SELECT id FROM chapters WHERE novel_id = ? AND number = ?",
        ("review-novel", 1),
    ) is None


@pytest.mark.parametrize("tamper", ["delete", "modify"])
def test_http_prose_tampered_stream_receipt_blocks_acceptance(prose_http, tamper):
    db, client, _provider, session_id = prose_http
    assert client.post(f"/ai-invocations/{session_id}/resume", json={}).status_code == 200
    generated = _wait_for_status(client, session_id, "awaiting_acceptance")
    attempt_id = generated["attempt"]["id"]
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            if tamper == "delete":
                conn.execute("DELETE FROM anti_ai_stream_runs WHERE job_id = ?", (attempt_id,))
            else:
                conn.execute(
                    "UPDATE anti_ai_stream_runs SET summary_json = '{}' WHERE job_id = ?",
                    (attempt_id,),
                )

    rejected = client.post(
        f"/ai-invocations/{session_id}/accept",
        json={"attempt_id": attempt_id, "accepted_by": "test"},
    )
    assert rejected.status_code == 409, rejected.text
    assert client.get(f"/ai-invocations/{session_id}").json()["session"]["status"] == "blocked"
    assert db.fetch_one(
        "SELECT id FROM chapters WHERE novel_id = ? AND number = ?",
        ("review-novel", 1),
    ) is None


@pytest.mark.parametrize("tamper", ["delete", "modify"])
def test_http_prose_tampered_prompt_receipt_blocks_commit(prose_http, tamper):
    db, client, _provider, session_id = prose_http
    assert client.post(f"/ai-invocations/{session_id}/resume", json={}).status_code == 200
    generated = _wait_for_status(client, session_id, "awaiting_acceptance")
    attempt_id = generated["attempt"]["id"]
    accepted = client.post(
        f"/ai-invocations/{session_id}/accept",
        json={"attempt_id": attempt_id, "accepted_by": "test"},
    )
    assert accepted.status_code == 200, accepted.text
    decision_id = accepted.json()["decision"]["id"]
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            if tamper == "delete":
                conn.execute("DELETE FROM anti_ai_prompt_runs WHERE job_id = ?", (attempt_id,))
            else:
                conn.execute(
                    "UPDATE anti_ai_prompt_runs SET evidence_json = '{}' WHERE job_id = ?",
                    (attempt_id,),
                )

    rejected = client.post(
        f"/ai-invocations/{session_id}/commits",
        json={"decision_id": decision_id},
    )
    assert rejected.status_code == 409, rejected.text
    assert client.get(f"/ai-invocations/{session_id}").json()["session"]["status"] == "blocked"
    assert db.fetch_one(
        "SELECT id FROM chapters WHERE novel_id = ? AND number = ?",
        ("review-novel", 1),
    ) is None


def test_prose_review_refresh_persists_the_exact_frozen_prompt_for_resume(prose_http):
    from application.ai_invocation.dtos import prompt_hash
    db, client, provider, session_id = prose_http
    with sqlite_writes_bypass_queue():
        with db.transaction() as conn:
            conn.execute("UPDATE unified_characters SET verbal_tic='给我三息' WHERE id='review-character'")
    reviewed = client.get(f"/ai-invocations/{session_id}").json()["session"]["prompt_snapshot"]
    assert "给我三息" in reviewed["prompt"]["system"]
    resumed = client.post(f"/ai-invocations/{session_id}/resume", json={})
    assert resumed.status_code == 200, resumed.text
    _wait_for_status(client, session_id, "awaiting_acceptance")
    assert prompt_hash(provider.prompts[-1]) == reviewed["rendered_prompt_hash"]


def test_prose_variable_update_returns_the_frozen_prompt_ready_for_review(prose_http):
    from application.ai_invocation.dtos import prompt_hash
    _db, client, provider, session_id = prose_http
    updated = client.put(f"/ai-invocations/{session_id}/variables", json={
        "values": {"chapter_outline": "林青在废墟找到旧信"}, "updated_by": "test",
    })
    assert updated.status_code == 200, updated.text
    reviewed = updated.json()["session"]["prompt_snapshot"]
    assert "【去味执行协议" in reviewed["prompt"]["system"]
    assert updated.json()["session"]["metadata"]["anti_ai_prompt"]["prompt_hash"] == reviewed["rendered_prompt_hash"]
    assert client.post(f"/ai-invocations/{session_id}/resume", json={}).status_code == 200
    _wait_for_status(client, session_id, "awaiting_acceptance")
    assert prompt_hash(provider.prompts[-1]) == reviewed["rendered_prompt_hash"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_http_prose_provider_close_error_never_becomes_success(prose_http, monkeypatch, cancelled):
    import asyncio
    db, client, _provider, session_id = prose_http

    class Source:
        def __init__(self):
            self.done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return "保留正文"

        async def aclose(self):
            if cancelled:
                raise asyncio.CancelledError("provider close cancelled")
            raise RuntimeError("provider close failed")

    class Provider:
        def stream_generate(self, prompt, config):
            return Source()

    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: Provider())
    assert client.post(f"/ai-invocations/{session_id}/resume", json={}).status_code == 200
    completed = _wait_for_status(client, session_id, "cancelled" if cancelled else "failed")
    assert completed["attempt"]["status"] == "failed"
    assert completed["attempt"]["content"] == "保留正文"
    run = db.fetch_one("SELECT status,error FROM anti_ai_stream_runs WHERE job_id=?", (completed["attempt"]["id"],))
    assert run["status"] == ("cancelled" if cancelled else "failed")
    assert "provider close" in run["error"]
