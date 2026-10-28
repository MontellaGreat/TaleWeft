"""Independent review: protected upstream inputs and concurrent variable writes."""
import json

import pytest

from application.onboarding.setup_edit_guard import SetupEditConflict
from application.world.services.character_manual_mutation import CharacterEditConflict
from tests.unit.application.services.test_character_manual_mutation import book, snapshot, writer  # noqa: F401


def test_rename_cannot_rewrite_upstream_contract_during_paused_worldbuilding_audit(book):
    _, db, bible, edits = book
    from application.onboarding.setup_dag import SetupDAGStore
    SetupDAGStore(db).ensure()
    db.execute("UPDATE novels SET premise='林舟守城。' WHERE id='book'")
    db.execute("INSERT INTO setup_dag_runs VALUES('paused-world','book','worldbuilding','paused',?,'2026-10-27')",
               (json.dumps({'workflow_version': 2}),))
    before = snapshot(bible)
    with pytest.raises((SetupEditConflict, CharacterEditConflict)):
        edits.update('book', 'hero', {'name': '沈砚'}, before)
    assert db.fetch_one("SELECT premise FROM novels WHERE id='book'")['premise'] == '林舟守城。'
    assert snapshot(bible) == before


def test_variable_change_after_snapshot_is_a_conflict_not_a_storage_failure(book, monkeypatch):
    _, db, bible, edits = book
    db.execute("INSERT INTO variable_definitions(id,variable_key,display_name,value_type,scope_level) VALUES('review-vdef','novel.premise','故事创意','string','novel')")
    db.execute("INSERT INTO variable_values(id,variable_key,scope_level,scope_key,value_json,version_number,is_current) VALUES('review-old','novel.premise','novel','novel_id:book',?,1,1)",
               (json.dumps('林舟的旧创意'),))
    original = edits._guard_snapshot
    raced = False
    def change_before_guard(table, where, params):
        nonlocal raced
        condition = original(table, where, params)
        if table == 'variable_values' and not raced:
            raced = True
            with db.transaction() as conn:
                conn.execute("UPDATE variable_values SET is_current=0 WHERE id='review-old'")
                conn.execute("INSERT INTO variable_values(id,variable_key,scope_level,scope_key,value_json,version_number,is_current) VALUES('review-new','novel.premise','novel','novel_id:book',?,2,1)",
                             (json.dumps('作者并发保存的新创意'),))
        return condition
    monkeypatch.setattr(edits, '_guard_snapshot', change_before_guard)
    before = snapshot(bible)
    with pytest.raises(CharacterEditConflict):
        edits.update('book', 'hero', {'name': '沈砚'}, before)
    assert raced
    assert snapshot(bible) == before
    assert json.loads(db.fetch_one("SELECT value_json FROM variable_values WHERE variable_key='novel.premise' AND is_current=1")['value_json']) == '作者并发保存的新创意'
