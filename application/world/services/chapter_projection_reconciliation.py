"""Source-aware rollback of chapter-generated mutable projections.

Only rows/fields captured by this version are owned here. Untagged historical
data is retained. Author changes that differ from the generated value win.
"""
import json
from contextlib import contextmanager

_TABLES = {
    'storylines': 'novel_id=?',
    'plot_points': 'plot_arc_id IN (SELECT id FROM plot_arcs WHERE novel_id=?)',
    'unified_props': 'novel_id=?',
}
_IGNORED = {'id', 'novel_id', 'created_at', 'updated_at', 'sort_order'}


def _snapshot(db, novel_id, tables):
    return {table: {r['id']: dict(r) for r in db.fetch_all(
        f'SELECT * FROM {table} WHERE {_TABLES[table]}', (novel_id,))}
        for table in tables}


def rollback_chapter_projections(db, novel_id, chapter_number):
    """Undo suffix in reverse order before analysing any revised chapter."""
    changes = db.fetch_all('SELECT * FROM chapter_projection_changes WHERE novel_id=? '
        'AND chapter_number>=? ORDER BY chapter_number DESC', (novel_id, chapter_number))
    current_rows = {}
    overrides = {(row['projection_table'], row['row_id']): json.loads(row['fields_json'])
        for row in db.fetch_all('SELECT * FROM chapter_projection_overrides WHERE novel_id=?', (novel_id,))}
    with db.transaction() as conn:
        for change in changes:
            table = change['projection_table']
            if table not in _TABLES:
                raise ValueError('Unknown chapter projection')
            before = json.loads(change['before_json']) if change['before_json'] else None
            after = json.loads(change['after_json']) if change['after_json'] else None
            key = (table, change['row_id'])
            if key not in current_rows:
                row = db.fetch_one(f'SELECT * FROM {table} WHERE id=?', (change['row_id'],))
                current_rows[key] = dict(row) if row else None
            current = current_rows[key]
            if current is None and after:
                conn.execute('INSERT INTO chapter_projection_overrides VALUES(?,?,?,?) '
                    'ON CONFLICT(novel_id,projection_table,row_id) DO UPDATE SET fields_json=excluded.fields_json',
                    (novel_id, table, change['row_id'], '{"__deleted__":true}'))
                continue
            if not after:
                continue
            fields = [key for key in after if key not in _IGNORED and (before is None or before.get(key) != after[key])]
            edited = {key: current.get(key) for key in fields if current.get(key) != after[key]}
            if edited:
                protected = overrides.setdefault((table, change['row_id']), {})
                protected.update(edited)
                conn.execute('INSERT INTO chapter_projection_overrides VALUES(?,?,?,?) '
                    'ON CONFLICT(novel_id,projection_table,row_id) DO UPDATE SET fields_json=excluded.fields_json',
                    (novel_id, table, change['row_id'], json.dumps(protected, ensure_ascii=False)))
            if before is None and not edited:
                conn.execute(f'DELETE FROM {table} WHERE id=?', (change['row_id'],))
                current_rows[key] = None
            elif before is not None:
                revert = [key for key in fields if key not in edited]
                if revert:
                    conn.execute(f'UPDATE {table} SET '+','.join(f'{key}=?' for key in revert)+' WHERE id=?',
                        tuple(before.get(key) for key in revert)+(change['row_id'],))
                    current.update({key: before.get(key) for key in revert})
        conn.execute('DELETE FROM chapter_projection_changes WHERE novel_id=? AND chapter_number>=?', (novel_id, chapter_number))
        # Machine prop events are a replaceable extraction; manual events stay.
        conn.execute("DELETE FROM prop_events WHERE novel_id=? AND chapter_number>=? AND source IN ('AUTO_PATTERN','AUTO_LLM')", (novel_id, chapter_number))
        conn.execute("DELETE FROM triples WHERE novel_id=? AND chapter_number>=? AND source_type='prop_event_auto' AND COALESCE(is_starred,0)=0", (novel_id, chapter_number))
        for row in db.fetch_all('SELECT event_id,mutations FROM narrative_events WHERE novel_id=? AND chapter_number>=?', (novel_id, chapter_number)):
            mutations = json.loads(row['mutations'] or '[]')
            if any(m.get('entity_type') == 'prop' and m.get('attribute') == 'last_event'
                and isinstance(m.get('value'), dict) and m['value'].get('source') in ('AUTO_PATTERN','AUTO_LLM') for m in mutations):
                conn.execute('DELETE FROM narrative_events WHERE event_id=?', (row['event_id'],))
        conn.execute('DELETE FROM prop_chapter_snapshots WHERE chapter_number>=? AND prop_id IN '
            '(SELECT id FROM unified_props WHERE novel_id=?)', (chapter_number, novel_id))


@contextmanager
def record_chapter_projection_changes(db, novel_id, chapter_number, tables):
    if db is None:
        yield
        return
    before = _snapshot(db, novel_id, tables)
    try:
        yield
    finally:
        # Staged transactions cannot read their own queued UPDATEs. Compute the
        # effective final rows in memory and journal exactly what will commit.
        after = _snapshot(db, novel_id, tables)
        protected_rows = db.fetch_all('SELECT * FROM chapter_projection_overrides WHERE novel_id=?', (novel_id,))
        with db.transaction() as conn:
            # Restore explicit author overrides after automatic projection writes.
            for protected in protected_rows:
                table = protected['projection_table']
                if table not in tables:
                    continue
                values = json.loads(protected['fields_json'])
                if values.get('__deleted__'):
                    conn.execute(f'DELETE FROM {table} WHERE id=?', (protected['row_id'],))
                    after[table].pop(protected['row_id'], None)
                    continue
                if values:
                    conn.execute(f'UPDATE {table} SET '+','.join(f'{key}=?' for key in values)+' WHERE id=?',
                        tuple(values.values())+(protected['row_id'],))
                    if protected['row_id'] in after[table]:
                        after[table][protected['row_id']].update(values)
            for table in tables:
                for row_id, row in after[table].items():
                    old = before[table].get(row_id)
                    if old is None or any(old.get(k) != v for k, v in row.items() if k not in _IGNORED):
                        conn.execute('INSERT INTO chapter_projection_changes VALUES(?,?,?,?,?,?) '
                            'ON CONFLICT(novel_id,chapter_number,projection_table,row_id) DO UPDATE SET after_json=excluded.after_json',
                            (novel_id, chapter_number, table, row_id,
                             json.dumps(old, ensure_ascii=False) if old else None, json.dumps(row, ensure_ascii=False)))
