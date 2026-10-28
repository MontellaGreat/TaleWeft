"""Keep fallback semantic-review CPMS lookups inside an isolated test DB."""
import pytest


@pytest.fixture(scope='session')
def semantic_fixture_manager(tmp_path_factory):
    from infrastructure.ai.prompt_manager import PromptManager
    from infrastructure.persistence.database.connection import DatabaseConnection
    db = DatabaseConnection(str(tmp_path_factory.mktemp('semantic-cpms') / 'prompts.sqlite'))
    mgr = PromptManager(db)
    assert mgr.ensure_seeded()
    yield mgr
    db.close_all(skip_checkpoint=True)


@pytest.fixture(autouse=True)
def isolated_semantic_rules(monkeypatch, semantic_fixture_manager):
    from application.audit.services.semantic_anti_ai import SemanticAntiAIReview
    original = SemanticAntiAIReview.freeze_current_rules

    def freeze(cls, *, prompt_manager=None, db=None):
        if prompt_manager is None and db is None:
            prompt_manager = semantic_fixture_manager
        return original(prompt_manager=prompt_manager, db=db)

    monkeypatch.setattr(SemanticAntiAIReview, 'freeze_current_rules', classmethod(freeze))
