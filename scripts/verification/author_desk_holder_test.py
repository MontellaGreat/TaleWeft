"""Regression: prop holders use the canonical character table and novel scope."""
import sqlite3
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi import HTTPException
from interfaces.api.v1.core.manuscript_entity_routes import _validate_holder
from infrastructure.persistence.database.manuscript_entity_repository import ManuscriptEntityRepository


class HolderTests(unittest.TestCase):
    def test_canonical_character_and_novel_scope(self):
        connection = sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE unified_characters (id TEXT, novel_id TEXT)')
        connection.execute("INSERT INTO unified_characters VALUES ('lin', 'fog')")

        class Database:
            def fetch_one(self, sql, params):
                return connection.execute(sql, params).fetchone()

        with patch('interfaces.api.v1.core.manuscript_entity_routes.get_database', return_value=Database()):
            _validate_holder('fog', 'lin')
            _validate_holder('fog', None)
            for novel, holder in [('fog', 'missing'), ('other-book', 'lin')]:
                with self.assertRaises(HTTPException) as raised:
                    _validate_holder(novel, holder)
                self.assertEqual(raised.exception.status_code, 422)
        connection.close()

    def test_props_crud_canonical_and_legacy(self):
        for canonical in (True, False):
            with self.subTest(canonical=canonical):
                connection = sqlite3.connect(':memory:')
                connection.row_factory = sqlite3.Row
                table = 'unified_props' if canonical else 'bible_props'
                extra = "introduced_chapter INTEGER, attributes_json TEXT NOT NULL DEFAULT '{}'" if canonical else 'first_chapter INTEGER, is_key INTEGER DEFAULT 0'
                connection.execute(f'CREATE TABLE {table} (id TEXT PRIMARY KEY, novel_id TEXT, name TEXT, description TEXT, aliases_json TEXT, holder_character_id TEXT, created_at TEXT, updated_at TEXT, {extra})')

                class Database:
                    def fetch_one(self, sql, params=()):
                        return connection.execute(sql, params).fetchone()
                    def fetch_all(self, sql, params=()):
                        return connection.execute(sql, params).fetchall()
                    def execute(self, sql, params=()):
                        return connection.execute(sql, params)
                    def get_connection(self):
                        return connection

                repo = ManuscriptEntityRepository(Database())
                prop = repo.create_prop('fog', name='铜钥匙', holder_character_id='lin', first_chapter=1)
                self.assertEqual(prop['first_chapter'], 1)
                self.assertEqual(repo.list_props('other'), [])
                if canonical:
                    connection.execute("UPDATE unified_props SET attributes_json = '{\"external\":42,\"key_context\":true,\"is_key\":false}'")
                    self.assertEqual(repo.get_prop('fog', prop['id'])['is_key'], 1)
                repo.update_prop('fog', prop['id'], name='黄铜钥匙', is_key=True, first_chapter=2)
                saved = repo.list_props('fog')[0]
                self.assertEqual((saved['name'], saved['is_key'], saved['first_chapter'], saved['holder_character_id']), ('黄铜钥匙', 1, 2, 'lin'))
                self.assertIsNone(repo.get_prop('other', prop['id']))
                if canonical:
                    self.assertEqual(connection.execute("SELECT json_extract(attributes_json, '$.external') FROM unified_props").fetchone()[0], 42)
                    self.assertEqual(connection.execute("SELECT json_extract(attributes_json, '$.key_context') FROM unified_props").fetchone()[0], 1)
                    # Legacy true must not override the canonical explicit false.
                    connection.execute("UPDATE unified_props SET attributes_json = json_set(attributes_json, '$.is_key', 1)")
                    repo.update_prop('fog', prop['id'], is_key=False)
                    self.assertEqual(repo.get_prop('fog', prop['id'])['is_key'], 0)
                    self.assertEqual(connection.execute("SELECT json_extract(attributes_json, '$.key_context') FROM unified_props").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT json_extract(attributes_json, '$.external') FROM unified_props").fetchone()[0], 42)
                repo.delete_prop('other', prop['id'])
                self.assertEqual(len(repo.list_props('fog')), 1)
                repo.delete_prop('fog', prop['id'])
                self.assertEqual(repo.list_props('fog'), [])
                connection.close()


if __name__ == '__main__':
    unittest.main()
