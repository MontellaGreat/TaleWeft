from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from domain.evolution.models import ChapterEvolutionSnapshot, EvolutionState, utcnow_iso
from domain.evolution.reducer import EvolutionReducer
from application.evolution.services.override_service import EvolutionOverrideService, PatchConflictError


class EvolutionSnapshotService:
    def __init__(
        self,
        snapshot_repository: Any,
        action_extractor: Any,
        reducer: Optional[EvolutionReducer] = None,
    ):
        self.snapshot_repository = snapshot_repository
        self.action_extractor = action_extractor
        self.reducer = reducer or EvolutionReducer()
        self.override_service = EvolutionOverrideService(snapshot_repository)

    def build_after_chapter_saved(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        branch_id: str = "main",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> ChapterEvolutionSnapshot:
        previous = self.snapshot_repository.get_latest_active_before(
            novel_id, branch_id, chapter_number
        )
        history_reader = getattr(self.snapshot_repository, 'get_latest_chapter_history', self.snapshot_repository.get_by_chapter)
        legacy_snapshot = history_reader(
            novel_id, branch_id, chapter_number
        )
        opening = previous.ending_state if previous else EvolutionState.empty()
        evidence_conflicts = []
        if evidence and evidence.get('evolution_errors'):
            evidence_conflicts.append({
                'conflict_type': 'EXTRACTION_EVIDENCE_INVALID', 'level': 'blocking',
                'message': 'chapter-end state extraction failed validation',
                'resolution_status': 'open', 'payload': {'errors': evidence['evolution_errors']},
            })
        try:
            actions = self.action_extractor.extract(novel_id, chapter_number, content, evidence=evidence)
        except (ValueError, TypeError) as exc:
            actions = []
            evidence_conflicts.append({
                'conflict_type': 'EXTRACTION_EVIDENCE_INVALID', 'level': 'blocking',
                'message': 'chapter-end action validation failed: ' + type(exc).__name__,
                'resolution_status': 'open', 'payload': {},
            })
        machine, errors = self.reducer.reduce(opening, actions)
        conflicts = evidence_conflicts + [e.to_dict() for e in errors]
        status = "blocked" if evidence_conflicts or any(e.level == "blocking" for e in errors) else "active"
        override_patches = list(getattr(legacy_snapshot, "human_override_patches", []) or [])
        ending = machine
        if override_patches and status != "blocked":
            try:
                ending = EvolutionState.from_dict(
                    self.override_service.apply_patch(machine.to_dict(), override_patches)
                )
            except PatchConflictError as exc:
                status = "blocked"
                conflicts.append(
                    {
                        "conflict_type": "PATCH_CONFLICT",
                        "level": "blocking",
                        "message": str(exc),
                        "resolution_status": "open",
                        "payload": {"patches": override_patches},
                    }
                )

        self.snapshot_repository.mark_stale_from(novel_id, branch_id, chapter_number)
        snapshot = ChapterEvolutionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            novel_id=novel_id,
            branch_id=branch_id,
            chapter_number=chapter_number,
            status=status,
            opening_state=opening,
            delta_actions=actions,
            machine_state=machine,
            human_override_patches=override_patches,
            ending_state=ending,
            source_refs=[{"type": "chapter", "chapter_number": chapter_number,
                          "chapter_end_state_coverage": (evidence or {}).get('evolution_state_coverage', 'missing')}],
            conflicts=conflicts,
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )
        self.snapshot_repository.save(snapshot)
        return snapshot

    def replay_from(self, novel_id: str, chapter_number: int, branch_id: str = "main") -> Dict[str, Any]:
        stale_count = self.snapshot_repository.mark_stale_from(novel_id, branch_id, chapter_number)
        return {
            "novel_id": novel_id,
            "branch_id": branch_id,
            "from_chapter": chapter_number,
            "stale_count": stale_count,
            "message": "snapshots marked stale; chapters will rebuild evolution snapshots when saved",
        }
