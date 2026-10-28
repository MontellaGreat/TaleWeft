"""Independent commit-boundary review of the v4 semantic evidence contract."""
import json
from types import SimpleNamespace

import pytest

from application.audit.services import anti_ai_audit
from application.audit.services.semantic_anti_ai import SemanticAntiAIReview as Review
from application.engine.dag.anti_ai_scan import scan_chapter
from application.engine.dag.chapter_services import DAGChapterServices
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.prompt_manager import PromptManager
from infrastructure.persistence.database.connection import DatabaseConnection
from tests.dag.setup_model_fixtures import approved_semantic_review


@pytest.fixture
def review_db(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "independent-semantic-review.sqlite"))
    manager = PromptManager(db)
    manager.ensure_seeded()
    manager.update_node(manager.get_node("anti-ai-chapter-audit").id,
                        system_prompt="独立复核：检查旁白提前替读者总结。")
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor",
                        lambda **_: anti_ai_audit.AntiAIAuditor())
    yield db, manager
    db.close_all(skip_checkpoint=True)


async def approved_state(db, manager):
    body = "守卫合上账本。"
    context = {"novel_id": "review-book", "chapter_number": 7,
               "outline": "守卫核对当班记录。", "is_final_chapter": False}

    async def invoke(prompt):
        return SimpleNamespace(content=json.dumps(
            approved_semantic_review(json.loads(prompt.user)), ensure_ascii=False))

    report = await Review.review(body, context=context, invoke=invoke,
                                 db=db, prompt_manager=manager)
    state = {"content": body, **context, **scan_chapter(body, {}).outputs,
             "semantic_anti_ai": report}
    assert DAGChapterServices._validate_anti_ai_review(state, db=db) == report
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("novel_id", "another-book"),
    ("chapter_number", 8),
    ("outline", "守卫决定隐瞒当班记录。"),
    ("is_final_chapter", True),
])
async def test_same_body_cannot_replay_durable_approval_in_changed_context(review_db, field, value):
    db, manager = review_db
    state = await approved_state(db, manager)
    state[field] = value
    with pytest.raises(RuntimeError, match="持久.*上下文"):
        DAGChapterServices._validate_anti_ai_review(state, db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["raw_response", "rehashed_prompt"])
async def test_commit_reopens_raw_response_and_prompt_instead_of_trusting_normalized_copy(review_db, tamper):
    db, manager = review_db
    state = await approved_state(db, manager)
    report = state["semantic_anti_ai"]
    row = db.execute("SELECT * FROM anti_ai_prompt_runs WHERE run_id = ?",
                     (report["evidence_id"],)).fetchone()
    evidence = json.loads(row["evidence_json"])
    prompt_hash = row["prompt_hash"]
    if tamper == "raw_response":
        evidence["raw_report"] = {"passed": True}
    else:
        evidence["prompt"]["system"] += "\n后加的非冻结规范。"
        prompt_hash = Review.prompt_digest(Prompt(**evidence["prompt"]))
    db.execute("UPDATE anti_ai_prompt_runs SET evidence_json = ?, prompt_hash = ? WHERE run_id = ?",
               (json.dumps(evidence, ensure_ascii=False), prompt_hash, report["evidence_id"]))
    db.commit()
    with pytest.raises(RuntimeError, match="持久"):
        DAGChapterServices._validate_anti_ai_review(state, db=db)
