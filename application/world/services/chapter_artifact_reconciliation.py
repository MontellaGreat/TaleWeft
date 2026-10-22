"""Replace machine-derived chapter evidence without erasing author decisions.

Only deterministic IDs/source tags owned by chapter extraction are replaced.
Older untagged user data and calibrated memory atoms remain authoritative.
"""
import json


EXTRACT_SOURCES = ('chapter_extract', 'dialogue_extract', 'triple_extract', 'causal_extract')


def replace_chapter_generated_artifacts(db, novel_id, chapter_number, bundle):
    if db is None:
        return
    from application.world.services.chapter_narrative_sync import _stable_artifact_id

    previous = db.fetch_one(
        'SELECT bundle_json FROM chapter_extraction_snapshots WHERE novel_id=? AND chapter_number=?',
        (novel_id, chapter_number),
    )
    previous_bundle = json.loads(previous['bundle_json']) if previous else {}
    old_arcs = []
    for wrapper in previous_bundle.get('character_mutations') or []:
        item = wrapper.get('data', wrapper) if isinstance(wrapper, dict) else {}
        if item.get('mutation_type') == 'emotional_arc':
            old_arcs.append((item.get('impact_or_description', ''), item.get('source_event', '')))

    with db.transaction() as conn:
        # Candidates are disposable read models. Confirm/reject actions survive
        # re-extraction, including identical evidence appearing in the new text.
        conn.execute("UPDATE memory_atoms SET status='superseded',updated_at=datetime('now') "
                     "WHERE novel_id=? AND chapter_number=? AND status='candidate' "
                     "AND source IN ('chapter_extract','dialogue_extract','triple_extract','causal_extract') "
                     "AND NOT EXISTS(SELECT 1 FROM memory_calibration_actions a WHERE a.atom_id=memory_atoms.id AND a.novel_id=memory_atoms.novel_id)",
                     (novel_id, chapter_number))
        conn.execute("DELETE FROM memory_projections WHERE novel_id=?", (novel_id,))
        conn.execute("DELETE FROM triples WHERE novel_id=? AND chapter_number=? "
                     "AND source_type='autopilot_extract' AND COALESCE(is_starred,0)=0", (novel_id, chapter_number))
        conn.execute("DELETE FROM causal_edges WHERE novel_id=? AND source_chapter=? AND id LIKE 'causal-%'", (novel_id, chapter_number))
        conn.execute("DELETE FROM narrative_debts WHERE novel_id=? AND planted_chapter=? AND id LIKE 'debt-%'", (novel_id, chapter_number))
        conn.execute("DELETE FROM narrative_events WHERE novel_id=? AND chapter_number=? AND event_id LIKE 'event-%'", (novel_id, chapter_number))
        # A deleted payoff must reopen the machine-created promise that it used
        # to settle. Author-created IDs are deliberately outside these updates.
        conn.execute("UPDATE causal_edges SET is_resolved=0,resolved_chapter=NULL WHERE novel_id=? AND resolved_chapter=? AND id LIKE 'causal-%'", (novel_id, chapter_number))
        conn.execute("UPDATE narrative_debts SET resolved_chapter=NULL WHERE novel_id=? AND resolved_chapter=? AND id LIKE 'debt-%'", (novel_id, chapter_number))
        registry_row = db.fetch_one('SELECT payload FROM novel_foreshadow_registry WHERE novel_id=?', (novel_id,))
        if registry_row:
            registry = json.loads(registry_row['payload'])
            registry['foreshadowings'] = [f for f in registry.get('foreshadowings', [])
                if not (str(f.get('id', '')).startswith('foreshadow-') and f.get('planted_in_chapter') == chapter_number)]
            for f in registry['foreshadowings']:
                if str(f.get('id', '')).startswith('foreshadow-') and f.get('resolved_in_chapter') == chapter_number:
                    f.update(status='planted', resolved_in_chapter=None)
            conn.execute('UPDATE novel_foreshadow_registry SET payload=?,updated_at=CURRENT_TIMESTAMP WHERE novel_id=?',
                         (json.dumps(registry, ensure_ascii=False), novel_id))
        conn.execute("DELETE FROM foreshadows WHERE novel_id=? AND planted_chapter=? AND id LIKE 'foreshadow-%'", (novel_id, chapter_number))
        conn.execute("UPDATE foreshadows SET status='planted',resolved_chapter=NULL WHERE novel_id=? AND resolved_chapter=? AND id LIKE 'foreshadow-%'", (novel_id, chapter_number))
        for row in db.fetch_all('SELECT * FROM character_states WHERE novel_id=?', (novel_id,)):
            scars = [s for s in json.loads(row['scars'] or '[]') if not (
                str(s.get('id', '')).startswith('scar-') and s.get('source_chapter') == chapter_number)]
            motivations = [m for m in json.loads(row['motivations'] or '[]') if not (
                str(m.get('id', '')).startswith('motivation-') and m.get('source_chapter') == chapter_number)]
            arcs = [a for a in json.loads(row['emotional_arc'] or '[]') if not (
                a.get('chapter') == chapter_number and (a.get('trigger') == '章末状态快照'
                    or (a.get('emotion', ''), a.get('trigger', '')) in old_arcs))]
            summary = row['current_state_summary']
            last_chapter = row['last_updated_chapter'] or 0
            if last_chapter == chapter_number:
                latest = max(arcs, key=lambda a: a.get('chapter', 0), default={})
                summary = latest.get('emotion', '')
                last_chapter = max([a.get('chapter', 0) for a in arcs] + [s.get('source_chapter', 0) for s in scars] + [m.get('source_chapter', 0) for m in motivations] + [0])
            conn.execute('UPDATE character_states SET scars=?,motivations=?,emotional_arc=?,current_state_summary=?,last_updated_chapter=? WHERE novel_id=? AND character_id=?',
                         (json.dumps(scars, ensure_ascii=False), json.dumps(motivations, ensure_ascii=False), json.dumps(arcs, ensure_ascii=False), summary, last_chapter, novel_id, row['character_id']))
        for item in previous_bundle.get('timeline_events') or []:
            if not isinstance(item, dict):
                continue
            note_id = _stable_artifact_id('tl', novel_id, chapter_number, str(item.get('time_point', '')).strip(),
                                         str(item.get('event', '')).strip(), str(item.get('description', '')).strip())
            conn.execute('DELETE FROM bible_timeline_notes WHERE novel_id=? AND id=?', (novel_id, note_id))
        conn.execute('INSERT INTO chapter_extraction_snapshots(novel_id,chapter_number,bundle_json) VALUES(?,?,?) '
                     'ON CONFLICT(novel_id,chapter_number) DO UPDATE SET bundle_json=excluded.bundle_json,updated_at=CURRENT_TIMESTAMP',
                     (novel_id, chapter_number, json.dumps(bundle, ensure_ascii=False)))


async def clear_chapter_generated_artifacts(db, indexing, novel_id, chapter_number):
    """An empty manuscript withdraws machine evidence without invoking a model."""
    import asyncio
    from application.core.services.chapter_version_service import content_hash
    await asyncio.to_thread(replace_chapter_generated_artifacts, db, novel_id, chapter_number, {})
    def clear_read_models():
        with db.transaction() as conn:
            conn.execute("DELETE FROM chapter_summaries WHERE chapter_number=? AND knowledge_id IN (SELECT id FROM knowledge WHERE novel_id=?)", (chapter_number, novel_id))
            for table in ('chapter_bridges', 'chapter_style_scores', 'chapter_guardrail_snapshots'):
                if db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                    conn.execute(f"DELETE FROM {table} WHERE novel_id=? AND chapter_number=?", (novel_id, chapter_number))
            # Preserve human patch provenance even when the manuscript is empty.
            conn.execute("UPDATE chapter_evolution_snapshots SET status='stale' WHERE novel_id=? AND chapter_number=?", (novel_id, chapter_number))
    await asyncio.to_thread(clear_read_models)
    if indexing is not None:
        versions = await asyncio.to_thread(db.fetch_all,
            "SELECT v.revision,v.content FROM content_versions v JOIN chapters c ON c.id=v.chapter_id WHERE c.novel_id=? AND c.number=?",
            (novel_id, chapter_number))
        await indexing.remove_chapter_summary(novel_id, chapter_number,
            [(v['revision'], content_hash(v['content'])) for v in versions])


def withdraw_chapter_suffix_evidence(db, novel_id, chapter_number):
    """Remove obsolete machine facts before any model reads the revised story.

    Validated author memories and untagged historical notes remain authoritative;
    chapter summaries keep their explicit stale state until rebuilt successfully.
    """
    rows = db.fetch_all('SELECT number FROM chapters WHERE novel_id=? AND number>=? ORDER BY number DESC',
                        (novel_id, chapter_number))
    for row in rows:
        replace_chapter_generated_artifacts(db, novel_id, row['number'], {})
    db.execute('UPDATE chapters SET tension_score=-1,plot_tension=-1,emotional_tension=-1,pacing_tension=-1 '
               'WHERE novel_id=? AND number>=?', (novel_id, chapter_number))
    db.commit()
