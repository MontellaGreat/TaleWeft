"""Immutable rule versions and exact scan snapshots in the existing SQLite writer."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from application.audit.services.cliche_scanner import AI_CLICHE_PATTERNS_ENHANCED
from application.audit.services.quality_rule_models import QualityRuleConfig
from infrastructure.persistence.database.connection import get_database


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class RuleVersionConflict(ValueError):
    pass


class QualityRuleStore:
    def __init__(self, db=None):
        self.db = db or get_database()
        with self.db.transaction() as writer:
            writer.execute('''CREATE TABLE IF NOT EXISTS quality_rule_versions (
                id TEXT PRIMARY KEY, config_json TEXT NOT NULL, note TEXT NOT NULL,
                source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            writer.execute('''CREATE TABLE IF NOT EXISTS quality_rule_active (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version_id TEXT NOT NULL)''')
            writer.execute('''CREATE TABLE IF NOT EXISTS quality_rule_snapshots (
                digest TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            writer.execute('''INSERT OR IGNORE INTO quality_rule_versions(id,config_json,note,source)
                VALUES ('builtin-v1', ?, '内置规则', 'builtin')''', (canonical_json(QualityRuleConfig().model_dump()),))
            writer.execute("INSERT OR IGNORE INTO quality_rule_active VALUES (1,'builtin-v1')")

    def current(self):
        row = self.db.fetch_one('''SELECT v.* FROM quality_rule_versions v
            JOIN quality_rule_active a ON a.version_id=v.id WHERE a.singleton=1''')
        return self._decode(row)

    @staticmethod
    def _decode(row):
        return {**{k: v for k, v in row.items() if k != 'config_json'}, 'config': json.loads(row['config_json'])}

    def list_versions(self):
        return [self._decode(row) for row in self.db.fetch_all(
            'SELECT rowid AS sequence,* FROM quality_rule_versions ORDER BY rowid DESC LIMIT 100')]

    def publish(self, config: QualityRuleConfig, *, expected_version: str, note: str, source='author'):
        names = {p[1] for p in AI_CLICHE_PATTERNS_ENHANCED}
        if set(config.disabled_builtin) - names:
            raise ValueError('禁用列表包含不存在的内置规则')
        if not note.strip() or len(note) > 500:
            raise ValueError('请填写不超过500字的版本说明')
        version = uuid4().hex
        with self.db.transaction() as writer:
            inserted = writer.execute('''INSERT INTO quality_rule_versions(id,config_json,note,source)
                SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM quality_rule_active WHERE singleton=1 AND version_id=?)''',
                (version, canonical_json(config.model_dump()), note, source, expected_version))
            writer.execute('UPDATE quality_rule_active SET version_id=? WHERE singleton=1 AND version_id=?',
                           (version, expected_version))
        if inserted.rowcount != 1:
            raise RuleVersionConflict('规则版本已变化，请刷新后保存')
        return self.current()

    def rollback(self, target_version: str, *, expected_version: str):
        target = self.db.fetch_one('SELECT * FROM quality_rule_versions WHERE id=?', (target_version,))
        if not target:
            raise LookupError('规则版本不存在')
        # Rollback creates a new immutable version; old reports remain reproducible.
        return self.publish(QualityRuleConfig.model_validate(json.loads(target['config_json'])),
            expected_version=expected_version, note=f'恢复版本 {target_version}', source=f'rollback:{target_version}')

    def effective(self):
        current = self.current()
        config = QualityRuleConfig.model_validate(current['config'])
        patterns = [list(p) for p in AI_CLICHE_PATTERNS_ENHANCED if p[1] not in config.disabled_builtin]
        for rule in config.rules:
            patterns.append([re.escape(rule.text), rule.name, rule.severity, rule.category, rule.replacement_hint])
        plugins = []
        if self.db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='signed_plugin_packages'"):
            for row in self.db.fetch_all('''SELECT p.id,p.digest,p.package_json FROM signed_plugin_packages p
                JOIN signed_plugin_keys k ON k.id=p.signer_id ORDER BY p.id'''):
                package = json.loads(row['package_json'])
                for rule in package['payload']['quality_rules']:
                    patterns.append([re.escape(rule['text']), f"{row['id']}: {rule['name']}",
                        rule['severity'], rule['category'], rule['replacement_hint']])
                plugins.append({'id': row['id'], 'digest': row['digest']})
        snapshot = {'policy_version': current['id'], 'patterns': patterns, 'plugins': plugins}
        serialized = canonical_json(snapshot)
        digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO quality_rule_snapshots(digest,snapshot_json) VALUES (?,?)', (digest, serialized))
        self.db.commit()
        return {'version': digest, **snapshot}

    def snapshot(self, digest):
        row = self.db.fetch_one('SELECT * FROM quality_rule_snapshots WHERE digest=?', (digest,))
        if not row:
            raise LookupError('扫描规则快照不存在')
        return {'digest': digest, 'created_at': row['created_at'], **json.loads(row['snapshot_json'])}

    def overview(self):
        return {'current': self.current(), 'versions': self.list_versions(), 'effective': self.effective(),
                'builtin': [{'name': p[1], 'severity': p[2], 'category': p[3], 'replacement_hint': p[4]}
                            for p in AI_CLICHE_PATTERNS_ENHANCED]}
