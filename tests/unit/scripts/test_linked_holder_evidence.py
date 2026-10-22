"""A new key number or historical handoff must not pass final-holder acceptance."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('linked_holder_evidence_under_test', ROOT / 'scripts/verification/linked_holder_evidence.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)
NAMES = {'沈舟': '沈舟', '林雁': '林雁', 'shen': '沈舟', 'lin': '林雁'}


@pytest.mark.parametrize('predicate', ['交还给', '曾保管', '原先持有', '不再保管'])
def test_historical_handoff_and_negated_ownership_are_not_final_state(predicate):
    assert driver.current_owner_claim({'subject': '沈舟', 'predicate': predicate, 'object': '林雁：九十二号验潮钥匙'}, NAMES) is None


def test_recalled_quote_about_ownership_is_not_current_state():
    assert driver.current_owner_claim({'summary': '他回忆此前说过：钥匙现在由林雁独自保管。'}, NAMES) is None


def make_database(*, legacy=False):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE chapters(novel_id TEXT,number INTEGER,revision INTEGER,content TEXT);
        INSERT INTO chapters VALUES('n',1,2,'钥匙现在由沈舟独自保管，林雁没有钥匙。编号九十二。');
        CREATE TABLE content_versions(novel_id TEXT,chapter_number INTEGER,revision INTEGER,content TEXT);
        INSERT INTO content_versions VALUES('n',1,1,'沈舟将编号七十四的验潮钥匙交给林雁，林雁收好钥匙。');
    ''')
    if legacy:
        return db
    db.executescript('''
        CREATE TABLE unified_characters(novel_id TEXT,id TEXT,name TEXT);
        INSERT INTO unified_characters VALUES('n','shen','沈舟'),('n','lin','林雁');
        CREATE TABLE memory_atoms(novel_id TEXT,id TEXT,chapter_number INTEGER,source TEXT,status TEXT,payload_json TEXT);
        CREATE TABLE chapter_extraction_snapshots(novel_id TEXT,chapter_number INTEGER,bundle_json TEXT);
        CREATE TABLE unified_props(novel_id TEXT,id TEXT,name TEXT,holder_character_id TEXT);
        INSERT INTO unified_props VALUES('n','key','验潮钥匙','lin');
        CREATE TABLE prop_chapter_snapshots(prop_id TEXT,chapter_number INTEGER,captured_at TEXT,holder_character_id TEXT);
        INSERT INTO prop_chapter_snapshots VALUES('key',1,'2026-10-16','shen');
        CREATE TABLE prop_events(novel_id TEXT,id TEXT,prop_id TEXT,chapter_number INTEGER,event_type TEXT,source TEXT,to_holder_id TEXT);
        CREATE TABLE chapter_projection_changes(novel_id TEXT,chapter_number INTEGER,projection_table TEXT,row_id TEXT,after_json TEXT);
        CREATE TABLE chapter_evolution_snapshots(novel_id TEXT,branch_id TEXT,chapter_number INTEGER,status TEXT,ending_state_json TEXT);
        CREATE TABLE memory_projections(novel_id TEXT,entity_id TEXT,projection_type TEXT,projection_json TEXT);
    ''')
    payloads = [
        {'subject': '沈舟', 'predicate': '保管', 'object': '九十二号验潮钥匙'},
        {'subject': '沈舟', 'predicate': '曾交给', 'object': '林雁：九十二号验潮钥匙'},
    ]
    for index, payload in enumerate(payloads):
        db.execute('INSERT INTO memory_atoms VALUES(?,?,?,?,?,?)', ('n', str(index), 1, 'triple_extract', 'candidate', json.dumps(payload, ensure_ascii=False)))
    state = {'items': {'key': {'name': '验潮钥匙', 'owner_id': 'shen'}},
        'characters': {'shen': {'inventory': ['key']}, 'lin': {'inventory': []}}}
    db.execute('INSERT INTO chapter_evolution_snapshots VALUES(?,?,?,?,?)', ('n', 'main', 1, 'active', json.dumps(state)))
    return db


def test_final_holder_check_uses_chapter_anchored_states_and_preserves_history():
    with make_database() as db:
        evidence = driver.inspect_holder(db, 'n', '沈舟', 92)
    assert evidence['original_generated_chapter']['old_74_number_present'] is True
    assert evidence['original_generated_chapter']['explicit_74_handoff_to_lin_yan_present'] is True
    assert evidence['active_automatic_memory']['final_owner_verified'] is True
    assert len(evidence['active_automatic_memory']['historical_handoffs_excluded_from_final_owner']) == 1
    # Global prop state is already at chapter 2 (Lin). Chapter 1 snapshot Shen
    # remains the correct anchor and must not create a false contradiction.
    assert evidence['chapter_anchored_prop_state']['book_current_holders_for_information_only'][0]['holder'] == '林雁'
    assert evidence['complete_holder_linkage_verified'] is True


def test_matching_number_without_final_ownership_is_not_sufficient():
    with make_database() as db:
        db.execute("DELETE FROM memory_atoms WHERE id='0'")
        evidence = driver.inspect_holder(db, 'n', '沈舟', 92)
    assert evidence['expected_number_in_current_body'] is True
    assert evidence['active_automatic_memory']['status'] == 'not_recorded'
    assert evidence['complete_holder_linkage_verified'] is False


def test_item_owner_and_character_inventory_must_agree():
    with make_database() as db:
        wrong = {'items': {'key': {'name': '验潮钥匙', 'owner_id': 'shen'}}, 'characters': {'lin': {'inventory': ['key']}}}
        db.execute('UPDATE chapter_evolution_snapshots SET ending_state_json=?', (json.dumps(wrong),))
        evidence = driver.inspect_holder(db, 'n', '沈舟', 92)
    assert evidence['chapter_evolution_item_owner']['final_owner_verified'] is True
    assert evidence['chapter_evolution_character_inventory']['status'] == 'different_owner_recorded'
    assert evidence['complete_holder_linkage_verified'] is False


def test_historical_stale_snapshot_cannot_establish_current_owner():
    with make_database() as db:
        db.execute("UPDATE chapter_evolution_snapshots SET status='stale'")
        evidence = driver.inspect_holder(db, 'n', '沈舟', 92)
    assert evidence['chapter_evolution_item_owner']['final_owner_verified'] is False
    assert evidence['complete_holder_linkage_verified'] is False


def test_older_schema_missing_linked_domains_reports_unestablished_not_success():
    with make_database(legacy=True) as db:
        evidence = driver.inspect_holder(db, 'n', '沈舟', 92)
    assert evidence['original_generated_chapter']['old_74_number_present'] is True
    assert evidence['expected_number_in_current_body'] is True
    assert evidence['chapter_evolution_item_owner']['status'] == 'not_recorded'
    assert evidence['complete_holder_linkage_verified'] is False
    assert {row['table'] for row in evidence['schema_unavailable']} >= {'memory_atoms', 'unified_props', 'chapter_evolution_snapshots'}


def test_registered_baseline_or_manual_event_cannot_impersonate_real_model_transfer():
    with make_database() as db:
        db.execute("INSERT INTO prop_events VALUES('n','event','key',1,'TRANSFERRED','MANUAL','shen')")
        evidence = driver.inspect_holder(db, 'n', '沈舟', 92)
    assert evidence['complete_holder_linkage_verified'] is True
    assert evidence['registered_prop_lifecycle']['registered_prop_lifecycle_verified'] is False


def test_real_model_transfer_and_chapter_state_both_required_for_registered_prop():
    with make_database() as db:
        db.execute("INSERT INTO prop_events VALUES('n','event','key',1,'TRANSFERRED','AUTO_LLM','shen')")
        evidence = driver.inspect_holder(db, 'n', '沈舟', 92)
        assert evidence['registered_prop_lifecycle']['registered_prop_lifecycle_verified'] is True
        db.execute("UPDATE prop_chapter_snapshots SET holder_character_id='lin'")
        wrong = driver.inspect_holder(db, 'n', '沈舟', 92)
    assert wrong['registered_prop_lifecycle']['registered_prop_lifecycle_verified'] is False
