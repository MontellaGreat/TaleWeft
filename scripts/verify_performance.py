#!/usr/bin/env python3
"""Benchmark real SQLite writer paths in a disposable project-local database.

Never reads get_database() without an explicit path, never inserts synthetic
commands into a user queue, and reports measured percentiles instead of claims.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def distribution(samples):
    ordered = sorted(samples)
    def percentile(fraction):
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]
    return {"samples": len(samples), "mean_ms": statistics.mean(samples),
            "p50_ms": percentile(.50), "p95_ms": percentile(.95), "p99_ms": percentile(.99)}


@contextmanager
def isolated_writer(db_path):
    from infrastructure.persistence.database import connection as connection_module
    from application.engine.services import persistence_queue as queue_module
    from application.engine.services.persistence_queue import PersistenceQueue, register_persistence_handlers
    from infrastructure.persistence.database.connection import DatabaseConnection
    saved_db, saved_instances = connection_module._db_instance, connection_module._db_instances_by_path
    saved_queue = queue_module._persistence_queue
    saved_env = {key: os.environ.get(key) for key in ('PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES', 'AITEXT_ALLOW_DIRECT_SQLITE_WRITES')}
    db = queue = None
    try:
        for key in saved_env:
            os.environ.pop(key, None)
        db = DatabaseConnection(str(db_path))
        connection_module._db_instance = db
        connection_module._db_instances_by_path = {str(db_path.resolve()): db}
        queue = PersistenceQueue()
        queue.initialize()
        queue_module._persistence_queue = queue
        register_persistence_handlers()
        queue.start_consumer()
        yield db, queue
    finally:
        if queue is not None:
            queue.stop_consumer()
            queue.get_queue().close()
            queue.get_queue().join_thread()
        if db is not None:
            db.close_all(skip_checkpoint=True)
        connection_module._db_instance, connection_module._db_instances_by_path = saved_db, saved_instances
        queue_module._persistence_queue = saved_queue
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_benchmark(*, runs=20, entity_rows=120, chapter_count=1000, work_root=None):
    from infrastructure.persistence.database.manuscript_entity_repository import ManuscriptEntityRepository
    work_root = Path(work_root or PROJECT_ROOT.parent / 'analysis' / '.benchmark-runs').resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    report = {"measured_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                  "os": platform.platform(), "cpu_count": os.cpu_count()},
              "fixture": {"runs": runs, "entities_per_chapter": entity_rows, "chapters": chapter_count,
                  "data": "synthetic Chinese manuscript, isolated SQLite + actual production writer ACK",
                  "models": "none", "network": "none", "project_user_data_touched": False}}
    with tempfile.TemporaryDirectory(prefix='plotpilot-bench-', dir=work_root) as directory:
        db_path = Path(directory) / 'isolated.sqlite'
        report['fixture']['database_path'] = str(db_path)
        start = time.perf_counter()
        with isolated_writer(db_path) as (db, queue):
            report['cold_schema_and_writer_ms'] = (time.perf_counter() - start) * 1000
            db.execute("INSERT INTO novels(id,title,slug) VALUES ('benchmark','Benchmark','benchmark')")
            with db.transaction() as conn:
                for number in range(1, chapter_count + 1):
                    conn.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES (?,?,?,?,?,'completed')",
                        (f'chapter-{number}', 'benchmark', number, f'第{number}章', '人物甲与人物乙讨论账本。' * 180))
            repository = ManuscriptEntityRepository(db)
            rows = [('character', f'char-{n}', f'人物{n}', n % 8 + 1) for n in range(entity_rows)]
            insert_sql = "INSERT INTO chapter_entity_mentions(novel_id,chapter_number,entity_kind,entity_id,display_label,mention_count,updated_at) VALUES (?,?,?,?,?,?,?)"

            def rowwise_baseline():
                db.execute("DELETE FROM chapter_entity_mentions WHERE novel_id=? AND chapter_number=?", ('benchmark', 1))
                for kind, entity_id, label, count in rows:
                    db.execute(insert_sql, ('benchmark', 1, kind, entity_id, label, count, '2026-10-12'))

            def batched_production():
                repository.replace_chapter_mentions('benchmark', 1, rows)

            for operation in (rowwise_baseline, batched_production):
                operation()  # warm indexes, imports and writer pipe
            baseline, batched = [], []
            baseline_commands, batched_commands = [], []
            tracemalloc.start()
            try:
                # Alternate order to reduce warm-cache/order bias.
                for iteration in range(runs):
                    operations = [(rowwise_baseline, baseline, baseline_commands), (batched_production, batched, batched_commands)]
                    if iteration % 2:
                        operations.reverse()
                    for operation, samples, commands in operations:
                        before = queue.get_stats()['processed']
                        started = time.perf_counter()
                        operation()
                        samples.append((time.perf_counter() - started) * 1000)
                        commands.append(queue.get_stats()['processed'] - before)
                        assert db.fetch_one("SELECT COUNT(*) AS count FROM chapter_entity_mentions WHERE novel_id='benchmark' AND chapter_number=1")['count'] == entity_rows
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            report['entity_index'] = {"baseline": distribution(baseline), "batched_production": distribution(batched),
                "baseline_writer_commands": statistics.mean(baseline_commands), "batched_writer_commands": statistics.mean(batched_commands),
                "p50_speedup": statistics.median(baseline) / statistics.median(batched), "peak_python_allocated_bytes": peak,
                "baseline_description": "equivalent DELETE + individual INSERTs through real writer; production now uses one atomic batch"}
            query = "SELECT id,revision,word_count FROM chapters WHERE novel_id=? AND number=?"
            lookups = []
            for index in range(runs * 10):
                started = time.perf_counter()
                found = db.fetch_one(query, ('benchmark', index % chapter_count + 1))
                lookups.append((time.perf_counter() - started) * 1000)
                assert found is not None
            report['chapter_lookup'] = distribution(lookups)
            report['chapter_lookup']['query_plan'] = db.fetch_all('EXPLAIN QUERY PLAN ' + query, ('benchmark', 1))
            concurrent = []
            def lookup(index):
                started = time.perf_counter()
                assert db.fetch_one(query, ('benchmark', index % chapter_count + 1))
                return (time.perf_counter() - started) * 1000
            with ThreadPoolExecutor(max_workers=4) as executor:
                concurrent.extend(executor.map(lookup, range(runs * 10)))
            report['concurrent_read_4_threads'] = distribution(concurrent)
            assert queue.wait_until_idle(timeout=10)
            report['writer'] = {"processed": queue.get_stats()['processed'], "failed": queue.get_stats()['failed']}
        report['fixture']['temporary_directory_removed_after_run'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--entities', type=int, default=120)
    parser.add_argument('--chapters', type=int, default=1000)
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT.parent / 'analysis' / 'performance-baseline.json')
    args = parser.parse_args()
    if not 3 <= args.runs <= 100 or not 1 <= args.entities <= 1000 or not 1 <= args.chapters <= 10000:
        parser.error('runs=3..100, entities=1..1000, chapters=1..10000')
    result = run_benchmark(runs=args.runs, entity_rows=args.entities, chapter_count=args.chapters)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(args.output.resolve()), 'entity_index': result['entity_index'],
                      'chapter_lookup': result['chapter_lookup'], 'writer': result['writer']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
