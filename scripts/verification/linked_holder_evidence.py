"""Read-only, chapter-anchored evidence for the live keyed-prop edit scenario.

This is a deliberately conservative acceptance oracle, not a general Chinese
ownership extractor. A handoff/history relation is not a final ownership claim.
Missing structured state remains missing, even if a new number occurs in prose.
Only isolated verification databases and story-domain tables are inspected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_novel_fullflow import ROOT, CheckFailed, read_json, readonly_db, require, write_json

NUMBERS = {74: '七十四', 92: '九十二', 107: '一百零七'}
PEOPLE = ('沈舟', '林雁')
SOURCES = "('chapter_extract','dialogue_extract','triple_extract','causal_extract')"
REQUIRED_COLUMNS = {
    'unified_characters': {'novel_id', 'id', 'name'},
    'chapters': {'novel_id', 'number', 'revision', 'content'},
    'content_versions': {'novel_id', 'chapter_number', 'revision', 'content'},
    'memory_atoms': {'novel_id', 'id', 'chapter_number', 'source', 'status', 'payload_json'},
    'chapter_extraction_snapshots': {'novel_id', 'chapter_number', 'bundle_json'},
    'unified_props': {'novel_id', 'id', 'name'},
    'prop_chapter_snapshots': {'prop_id', 'chapter_number', 'captured_at', 'holder_character_id'},
    'prop_events': {'novel_id', 'prop_id', 'chapter_number', 'event_type', 'source', 'to_holder_id'},
    'chapter_projection_changes': {'novel_id', 'chapter_number', 'projection_table', 'row_id', 'after_json'},
    'chapter_evolution_snapshots': {'novel_id', 'branch_id', 'chapter_number', 'ending_state_json'},
    'memory_projections': {'novel_id', 'entity_id', 'projection_type', 'projection_json'},
}
CURRENT_VERBS = {'持有', '拥有', '保管', '持有着', '保管着', '持有钥匙', '独自保管', '当前持有', '目前持有', '现在持有', '现持有', '当前保管', '目前保管', '现在保管'}
INVERSE_VERBS = {'持有者', '保管者', '保管人', '归属', '归属于', '现归属', '当前持有者', '当前保管人', '由谁保管'}
HISTORY = ('交给', '交还', '交付', '移交', '归还', '曾', '此前', '原先', '起初', '先前', '过去')


def numbered(text, number):
    return NUMBERS[number] in text or bool(re.search(r'(?<!\d)' + str(number) + r'(?!\d)', text))


def key_reference(text):
    return '钥匙' in str(text)


def current_owner_claim(payload, names):
    """Accept explicit state relations only; never turn a transfer into state."""
    if not isinstance(payload, dict):
        return None
    subject, predicate, obj = (str(payload.get(field) or '').strip() for field in ('subject', 'predicate', 'object'))
    if any(word in predicate for word in HISTORY):
        return None
    if predicate in CURRENT_VERBS and key_reference(obj) and subject in names:
        return {'owner': names[subject], 'key_reference': obj, 'kind': 'explicit_current_relation'}
    if predicate in INVERSE_VERBS and key_reference(subject) and obj in names:
        return {'owner': names[obj], 'key_reference': subject, 'kind': 'explicit_current_relation'}
    # State summaries may assert final ownership explicitly. Dialogue/causal
    # content is intentionally excluded: it can contain quotes, plans or claims.
    for field in ('mental_state', 'current_state_summary', 'summary'):
        text = str(payload.get(field) or '')
        for owner in PEOPLE:
            exact = re.search(r'(?:钥匙)(?:现在|目前|最终|当前)?由' + re.escape(owner) + r'(?:独自)?保管', text)
            if exact and not any(word in text[max(0, exact.start() - 20):exact.start()] for word in HISTORY + ('回忆', '计划', '希望', '打算', '说过')):
                return {'owner': owner, 'key_reference': exact.group(0), 'kind': 'explicit_current_summary'}
    return None


def claim_summary(claims, expected_owner):
    owners = sorted({claim['owner'] for claim in claims})
    if expected_owner in owners and len(owners) == 1:
        status = 'expected_owner_recorded'
    elif owners and expected_owner not in owners:
        status = 'different_owner_recorded'
    elif len(owners) > 1:
        status = 'ambiguous_current_owner_claims'
    else:
        status = 'not_recorded'
    return {'status': status, 'owners': owners, 'records': claims,
        'final_owner_verified': status == 'expected_owner_recorded'}


def inspect_holder(connection, novel, expected_owner, expected_number):
    """SELECT only. Caller owns a SQLite connection opened with mode=ro."""
    require(expected_owner in PEOPLE and expected_number in NUMBERS, 'Unknown keyed-prop acceptance expectation')
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    schema_gaps = {}
    def rows(sql, parameters=(novel,)):
        # Old source generations may lack extraction journals or whole domains.
        # Absence is evidence of an unestablished link, never a successful check.
        table = re.search(r'\bFROM\s+([a-z_]+)', sql, re.IGNORECASE).group(1)
        require(table in REQUIRED_COLUMNS, 'Inspection tried to access an unsupported story-domain table')
        if table not in tables:
            schema_gaps[table] = {'table': table, 'status': 'missing_table'}
            return []
        columns = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
        missing = REQUIRED_COLUMNS[table] - columns
        if missing:
            schema_gaps[table] = {'table': table, 'status': 'missing_columns', 'columns': sorted(missing)}
            return []
        return [dict(row) for row in connection.execute(sql, parameters)]
    characters = rows('SELECT * FROM unified_characters WHERE novel_id=?')
    names = {person: person for person in PEOPLE}
    names.update({row['id']: row['name'] for row in characters})
    first = rows('SELECT * FROM chapters WHERE novel_id=? AND number=1')
    require(bool(first), 'Isolated novel has no first chapter')
    first = first[0]
    original = rows("SELECT * FROM content_versions WHERE novel_id=? AND chapter_number=1 AND trim(content)<>'' ORDER BY revision LIMIT 1")
    original = original[0] if original else None
    original_text = original['content'] if original else ''
    original_key_sentences = [sentence.strip()[:240] for sentence in re.split(r'(?<=[。！？\n])', original_text) if key_reference(sentence) or numbered(sentence, 74)]
    baseline = {
        'status': 'recorded' if original else 'not_recorded',
        'revision': original['revision'] if original else None,
        'content_hash': (original.get('content_hash') or hashlib.sha256(original_text.encode('utf-8')).hexdigest()) if original else None,
        'old_74_number_present': numbered(original_text, 74) if original else None,
        'explicit_74_handoff_to_lin_yan_present': bool(re.search(r'沈舟将编号(?:七十四|74)的验潮钥匙交给林雁', original_text)) if original else None,
        'key_sentences_for_author_review': original_key_sentences,
        'final_owner_inferred_from_historical_handoff': False,
    }
    atoms = rows(f"SELECT * FROM memory_atoms WHERE novel_id=? AND chapter_number=1 AND source IN {SOURCES} AND status IN ('candidate','confirmed')")
    memory_claims, history_events = [], []
    for atom in atoms:
        payload = json.loads(atom['payload_json'])
        claim = current_owner_claim(payload, names)
        if claim:
            memory_claims.append({**claim, 'atom_id': atom['id'], 'source': atom['source'], 'status': atom['status'],
                'expected_number_explicit': numbered(json.dumps(payload, ensure_ascii=False), expected_number)})
        if isinstance(payload, dict) and any(word in str(payload.get('predicate') or '') for word in HISTORY) and key_reference(json.dumps(payload, ensure_ascii=False)):
            history_events.append({'atom_id': atom['id'], 'subject': payload.get('subject'), 'predicate': payload.get('predicate'), 'object': payload.get('object')})
    memory_evidence = claim_summary(memory_claims, expected_owner)
    memory_evidence['historical_handoffs_excluded_from_final_owner'] = history_events

    extraction_rows = rows('SELECT * FROM chapter_extraction_snapshots WHERE novel_id=? AND chapter_number=1')
    bundle = json.loads(extraction_rows[0]['bundle_json']) if extraction_rows else {}
    extraction_claims = []
    for index, wrapped in enumerate(bundle.get('relation_triples') or []):
        payload = wrapped.get('data', wrapped)
        claim = current_owner_claim(payload, names)
        if claim:
            extraction_claims.append({**claim, 'field': 'relation_triples', 'index': index,
                'expected_number_explicit': numbered(json.dumps(payload, ensure_ascii=False), expected_number)})
    for index, payload in enumerate(bundle.get('character_states') or []):
        claim = current_owner_claim(payload, names)
        if claim:
            extraction_claims.append({**claim, 'field': 'character_states', 'index': index,
                'expected_number_explicit': numbered(json.dumps(payload, ensure_ascii=False), expected_number)})
    extraction_evidence = claim_summary(extraction_claims, expected_owner)

    props = rows('SELECT * FROM unified_props WHERE novel_id=?')
    key_props = [prop for prop in props if key_reference(prop['name'] + (prop.get('description') or '') + (prop.get('aliases_json') or ''))]
    labels = {prop['id']: prop['name'] for prop in key_props}
    anchored_prop_claims = []
    for prop in key_props:
        snapshots = rows('SELECT * FROM prop_chapter_snapshots WHERE prop_id=? AND chapter_number=1 ORDER BY captured_at DESC LIMIT 1', (prop['id'],))
        journal = rows("SELECT * FROM chapter_projection_changes WHERE novel_id=? AND chapter_number=1 AND projection_table='unified_props' AND row_id=?", (novel, prop['id']))
        for field, holder in [('prop_chapter_snapshots', snapshots[0]['holder_character_id'] if snapshots else None),
                              ('chapter_projection_changes', json.loads(journal[0]['after_json'] or '{}').get('holder_character_id') if journal else None)]:
            if holder:
                anchored_prop_claims.append({'owner': names.get(holder, holder), 'key_reference': prop['name'], 'prop_id': prop['id'], 'field': field})
    prop_evidence = claim_summary(anchored_prop_claims, expected_owner)
    prop_evidence['key_prop_records'] = len(key_props)
    prop_evidence['book_current_holders_for_information_only'] = [{'prop_id': prop['id'], 'key_reference': prop['name'],
        'holder': names.get(prop.get('holder_character_id'), prop.get('holder_character_id'))} for prop in key_props]
    prop_evidence['comparison_scope'] = 'Chapter 1 snapshots/journal only; global prop state may include legitimate later transfers.'
    prop_events = rows('SELECT * FROM prop_events WHERE novel_id=? AND chapter_number=1 ORDER BY rowid')
    registered_prop_ids = set(labels)
    automatic_transfers = [{'event_id': event.get('id'), 'prop_id': event['prop_id'], 'source': event['source'],
        'to_holder': names.get(event.get('to_holder_id'), event.get('to_holder_id'))} for event in prop_events
        if event['prop_id'] in registered_prop_ids and event['event_type'] == 'TRANSFERRED' and event['source'] == 'AUTO_LLM']
    registered_lifecycle = {
        'automatic_LLM_transfers': automatic_transfers,
        'expected_final_holder_in_automatic_LLM_transfer': any(event['to_holder'] == expected_owner for event in automatic_transfers),
        'final_holder_in_chapter_prop_state': prop_evidence['final_owner_verified'],
        'registered_prop_lifecycle_verified': prop_evidence['final_owner_verified'] and any(event['to_holder'] == expected_owner for event in automatic_transfers),
        'automatic_catalogue_discovery_verified': False,
    }

    evolution_rows = rows("SELECT * FROM chapter_evolution_snapshots WHERE novel_id=? AND branch_id='main' AND chapter_number=1 ORDER BY rowid DESC LIMIT 1")
    ending = json.loads(evolution_rows[0]['ending_state_json']) if evolution_rows else {}
    evolution_claims, inventory = [], []
    key_item_ids = set(labels)
    for item_id, item in (ending.get('items') or {}).items():
        label = labels.get(item_id, item.get('name') or item_id)
        if key_reference(label):
            key_item_ids.add(item_id)
            holder = item.get('owner_id')
            if holder:
                evolution_claims.append({'owner': names.get(holder, holder), 'key_reference': label, 'item_id': item_id, 'field': 'items.owner_id'})
    for character_id, character in (ending.get('characters') or {}).items():
        for item_id in character.get('inventory') or []:
            if item_id in key_item_ids or key_reference(item_id):
                inventory.append({'owner': names.get(character_id, character_id), 'key_reference': labels.get(item_id, item_id), 'item_id': item_id, 'field': 'characters.inventory'})
    evolution_evidence = claim_summary(evolution_claims, expected_owner)
    evolution_evidence['snapshot_status'] = evolution_rows[0].get('status') if evolution_rows else None
    if evolution_evidence['snapshot_status'] != 'active':
        evolution_evidence['final_owner_verified'] = False
    inventory_evidence = claim_summary(inventory, expected_owner)
    if evolution_evidence['snapshot_status'] != 'active':
        inventory_evidence['final_owner_verified'] = False

    character_summaries = []
    for character in characters:
        if character.get('last_updated_chapter') == 1:
            claim = current_owner_claim({'current_state_summary': character.get('current_state_summary')}, names)
            if claim:
                character_summaries.append({**claim, 'character_id': character['id']})
    projections = rows('SELECT * FROM memory_projections WHERE novel_id=?')
    projection_evidence = {'persisted_records': len(projections),
        'status': 'present_not_rebuilt_by_read_only_check' if projections else 'not_materialized',
        'final_owner_verified': False,
        'limitation': 'Character projections are lazy caches. This check never calls getters that write or import memory.'}
    explicit_tail = '钥匙现在由' + expected_owner + '独自保管' in first['content'][-400:]
    complete = all(section['final_owner_verified'] for section in (memory_evidence, prop_evidence, evolution_evidence, inventory_evidence))
    return {
        'scope': 'chapter_1_ending_state_after_author_edit',
        'expected_final_owner': expected_owner,
        'expected_key_number': expected_number,
        'original_generated_chapter': baseline,
        'current_chapter_revision': first['revision'],
        'target_edit_present': explicit_tail and numbered(first['content'], expected_number),
        'explicit_expected_final_owner_in_edited_tail': explicit_tail,
        'expected_number_in_current_body': numbered(first['content'], expected_number),
        'active_automatic_memory': memory_evidence,
        'extraction_snapshot_current_claims': extraction_evidence,
        'chapter_anchored_prop_state': prop_evidence,
        'registered_prop_lifecycle': registered_lifecycle,
        'chapter_evolution_item_owner': evolution_evidence,
        'chapter_evolution_character_inventory': inventory_evidence,
        'chapter_anchored_character_summary': claim_summary(character_summaries, expected_owner),
        'existing_character_projections': projection_evidence,
        'complete_holder_linkage_verified': complete and explicit_tail and numbered(first['content'], expected_number)
            and baseline['old_74_number_present'] is True,
        'schema_unavailable': sorted(schema_gaps.values(), key=lambda row: row['table']),
        'coverage_gaps': [name for name, section in [('active_automatic_memory', memory_evidence),
            ('chapter_anchored_prop_state', prop_evidence), ('chapter_evolution_item_owner', evolution_evidence),
            ('chapter_evolution_character_inventory', inventory_evidence)] if not section['final_owner_verified']],
        'limitation': 'This conservative oracle verifies explicit current state only. Historical transfers remain valid evidence and never establish the final owner on their own.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--expected-owner', choices=PEOPLE, required=True)
    parser.add_argument('--expected-number', type=int, choices=NUMBERS, required=True)
    parser.add_argument('--report-name', default='holder-evidence.json')
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    verification = ROOT / 'out' / 'verification'
    require(directory.is_relative_to(verification) and directory != verification,
        'Read-only inspection requires an isolated run below project out/verification')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]+\.json', args.report_name)), 'Report name must be a simple JSON filename')
    state = read_json(directory / 'checkpoint.json') or read_json(directory / 'browser-result.json') or read_json(directory / 'result.json') or {}
    novel = state.get('novel_id')
    require(bool(novel), 'Isolated run has no saved novel ID')
    with readonly_db(directory / 'data' / 'plotpilot.db') as connection:
        # A read transaction makes the report a coherent snapshot even while
        # an independently owned live workflow is advancing.
        connection.execute('BEGIN')
        evidence = inspect_holder(connection, novel, args.expected_owner, args.expected_number)
    evidence = {'status': 'read_only_evidence_NOT_live_generation_acceptance', 'model_calls': 0, 'novel_id': novel, **evidence}
    write_json(directory / args.report_name, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except CheckFailed as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
