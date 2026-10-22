"""Ed25519-signed data packages. No modules, file paths, hooks or network grants."""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from application.audit.services.quality_rule_models import LiteralQualityRule
from infrastructure.ai.quality_rule_store import canonical_json
from infrastructure.persistence.database.connection import get_database

Permission = Literal['prompts', 'quality_rules']


class PluginPrompt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    key: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    category: Literal['generation', 'extraction', 'review', 'planning', 'world', 'creative', 'anti-ai'] = 'generation'
    system_prompt: str = Field(default='', max_length=50000)
    user_template: str = Field(default='', max_length=50000)

    @field_validator('system_prompt', 'user_template')
    @classmethod
    def plain_variables_only(cls, value):
        # The existing CPMS supports executable Jinja expressions. Imported
        # packages deliberately accept only simple named placeholders.
        if '{%' in value or '{#' in value:
            raise ValueError('插件提示词仅支持简单变量，不支持模板指令')
        stripped = re.sub(r'\{\{\s*[A-Za-z][A-Za-z0-9_]*\s*\}\}', '', value)
        if '{{' in stripped or '}}' in stripped:
            raise ValueError('插件提示词仅支持 {variable} 或 {{ variable }}')
        return value


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r'^\d{1,4}\.\d{1,4}\.\d{1,4}$')
    signer_id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    permissions: list[Permission] = Field(max_length=2)

    @field_validator('permissions')
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('权限声明不可重复')
        return value


class PluginPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    prompts: list[PluginPrompt] = Field(default_factory=list, max_length=50)
    quality_rules: list[LiteralQualityRule] = Field(default_factory=list, max_length=100)


class SignedPluginPackage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    manifest: PluginManifest
    payload: PluginPayload
    signature: str = Field(min_length=80, max_length=100)

    def signing_bytes(self):
        return canonical_json({'manifest': self.manifest.model_dump(), 'payload': self.payload.model_dump()}).encode('utf-8')


class PluginRejectedError(ValueError):
    pass


def _base64(value, size):
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise PluginRejectedError('无效的 Base64 密钥或签名') from exc
    if len(decoded) != size:
        raise PluginRejectedError('密钥或签名长度不正确')
    return decoded


class SignedPluginStore:
    def __init__(self, db=None):
        self.db = db or get_database()
        with self.db.transaction() as writer:
            writer.execute('''CREATE TABLE IF NOT EXISTS signed_plugin_keys (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, public_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, permissions_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            writer.execute('''CREATE TABLE IF NOT EXISTS signed_plugin_packages (
                id TEXT PRIMARY KEY, version TEXT NOT NULL, signer_id TEXT NOT NULL,
                digest TEXT NOT NULL, package_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(signer_id) REFERENCES signed_plugin_keys(id))''')
            writer.execute('''CREATE TABLE IF NOT EXISTS signed_plugin_resources (
                plugin_id TEXT PRIMARY KEY NOT NULL,
                template_id TEXT NOT NULL,
                FOREIGN KEY(plugin_id) REFERENCES signed_plugin_packages(id) ON DELETE CASCADE,
                FOREIGN KEY(template_id) REFERENCES prompt_templates(id) ON DELETE CASCADE)''')

    def keys(self):
        import json
        return [{**{k: v for k, v in row.items() if k != 'permissions_json'},
                 'permissions': json.loads(row['permissions_json'])}
                for row in self.db.fetch_all('SELECT * FROM signed_plugin_keys ORDER BY id')]

    def trust(self, *, id: str, name: str, public_key: str, permissions: list[Permission]):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', id) or not name.strip() or len(name) > 100:
            raise PluginRejectedError('发布者标识或名称无效')
        if not set(permissions) <= {'prompts', 'quality_rules'} or len(set(permissions)) != len(permissions):
            raise PluginRejectedError('发布者权限无效')
        raw = _base64(public_key, 32)
        Ed25519PublicKey.from_public_bytes(raw)
        try:
            self.db.execute('''INSERT INTO signed_plugin_keys(id,name,public_key,fingerprint,permissions_json)
                VALUES (?,?,?,?,?)''', (id, name, base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest(), canonical_json(permissions)))
            self.db.commit()
        except Exception as exc:
            if self.db.fetch_one('SELECT id FROM signed_plugin_keys WHERE id=?', (id,)):
                raise PluginRejectedError('发布者已存在；更换密钥或权限前请先移除信任') from exc
            raise
        return next(key for key in self.keys() if key['id'] == id)

    def verify(self, package: SignedPluginPackage):
        import json
        body = package.signing_bytes()
        if len(body) > 2_000_000:
            raise PluginRejectedError('插件包超过2MB限制')
        key = self.db.fetch_one('SELECT * FROM signed_plugin_keys WHERE id=?', (package.manifest.signer_id,))
        if not key:
            raise PluginRejectedError('未信任该发布者；请先核对公钥指纹并添加信任')
        permissions = set(package.manifest.permissions)
        if not permissions <= set(json.loads(key['permissions_json'])):
            raise PluginRejectedError('插件声明的权限超出发布者授权')
        required = set()
        if package.payload.prompts:
            required.add('prompts')
        if package.payload.quality_rules:
            required.add('quality_rules')
        if permissions != required or not required:
            raise PluginRejectedError('权限声明必须准确对应包内提示词和质量规则')
        if len({p.key for p in package.payload.prompts}) != len(package.payload.prompts):
            raise PluginRejectedError('插件提示词标识重复')
        if len({p.name for p in package.payload.quality_rules}) != len(package.payload.quality_rules):
            raise PluginRejectedError('插件质量规则名称重复')
        try:
            Ed25519PublicKey.from_public_bytes(_base64(key['public_key'], 32)).verify(_base64(package.signature, 64), body)
        except InvalidSignature as exc:
            raise PluginRejectedError('插件签名验证失败，内容可能已变更') from exc
        return {'id': package.manifest.id, 'name': package.manifest.name, 'version': package.manifest.version,
            'signer_id': package.manifest.signer_id, 'fingerprint': key['fingerprint'],
            'permissions': package.manifest.permissions, 'digest': hashlib.sha256(body).hexdigest(),
            'prompt_count': len(package.payload.prompts), 'rule_count': len(package.payload.quality_rules), 'verified': True}

    def list_packages(self):
        import json
        result = []
        for row in self.db.fetch_all('SELECT * FROM signed_plugin_packages ORDER BY created_at DESC,id'):
            package = SignedPluginPackage.model_validate(json.loads(row['package_json']))
            result.append({**package.manifest.model_dump(), 'digest': row['digest'], 'created_at': row['created_at'],
                'prompt_count': len(package.payload.prompts), 'rule_count': len(package.payload.quality_rules)})
        return result

    @staticmethod
    def template_id(plugin_id):
        return f'signed-plugin:{plugin_id}'

    def install(self, package: SignedPluginPackage, *, expected_digest: str):
        preview = self.verify(package)
        if preview['digest'] != expected_digest:
            raise PluginRejectedError('安装包与预览版本不一致，请重新验证')
        pid = package.manifest.id
        if self.db.fetch_one('SELECT id FROM signed_plugin_packages WHERE id=?', (pid,)):
            raise PluginRejectedError('插件已安装；升级前请先卸载旧版本')
        # Recheck key and granted permission in the writer transaction. The
        # foreign key also blocks a revoke racing installation.
        key = next((item for item in self.keys() if item['id'] == package.manifest.signer_id), None)
        if key is None or key['fingerprint'] != preview['fingerprint'] or not set(package.manifest.permissions) <= set(key['permissions']):
            raise PluginRejectedError('发布者信任已变化，请重新验证')
        tid = self.template_id(pid)
        try:
            with self.db.transaction() as writer:
                writer.execute('''INSERT INTO signed_plugin_packages(id,version,signer_id,digest,package_json)
                    SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM signed_plugin_keys
                    WHERE id=? AND public_key=? AND permissions_json=?)''',
                    (pid, package.manifest.version, package.manifest.signer_id, preview['digest'], canonical_json(package.model_dump()),
                     key['id'], key['public_key'], canonical_json(key['permissions'])))
                # The dependent insert intentionally fails if revocation won.
                writer.execute('''INSERT INTO prompt_templates(id,name,description,category,version,author,metadata)
                    VALUES (?, ?, '', 'user', ?, ?, ?)''',
                    (tid, package.manifest.name, package.manifest.version, package.manifest.signer_id, canonical_json({'signed_plugin': pid})))
                for prompt in package.payload.prompts:
                    node_id, version_id = uuid_hex(), uuid_hex()
                    variables = sorted(set(re.findall(r'\{\{?\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}?', prompt.system_prompt + prompt.user_template)))
                    writer.execute('''INSERT INTO prompt_nodes(id,template_id,node_key,name,category,source,variables,active_version_id)
                        SELECT ?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM signed_plugin_packages WHERE id=?)''',
                        (node_id, tid, f'plugin-{pid}-{prompt.key}', prompt.name, prompt.category, f'signed-plugin:{pid}',
                         canonical_json([{'name': name, 'type': 'string', 'scope': 'chapter'} for name in variables]), version_id, pid))
                    writer.execute('''INSERT INTO prompt_versions(id,node_id,version_number,system_prompt,user_template,change_summary,created_by)
                        VALUES (?,?,1,?,?,?,?)''', (version_id, node_id, prompt.system_prompt, prompt.user_template,
                            f'签名插件 {pid} {package.manifest.version}', f'plugin:{pid}'))
                # Ensure a revoked/no-prompt package cannot leave an orphan.
                writer.execute('INSERT INTO signed_plugin_resources(plugin_id,template_id) VALUES (?,?)', (pid, tid))
        except Exception as exc:
            raise PluginRejectedError('插件安装未提交；插件标识冲突、信任已变化或存储失败') from exc
        self._invalidate()
        return preview

    def uninstall(self, plugin_id):
        if not self.db.fetch_one('SELECT id FROM signed_plugin_packages WHERE id=?', (plugin_id,)):
            raise LookupError('插件不存在')
        with self.db.transaction() as writer:
            writer.execute('DELETE FROM prompt_templates WHERE id=?', (self.template_id(plugin_id),))
            writer.execute('DELETE FROM signed_plugin_packages WHERE id=?', (plugin_id,))
        self._invalidate()

    def revoke(self, signer_id):
        with self.db.transaction() as writer:
            writer.execute('''DELETE FROM prompt_templates WHERE id IN
                (SELECT 'signed-plugin:' || id FROM signed_plugin_packages WHERE signer_id=?)''', (signer_id,))
            writer.execute('DELETE FROM signed_plugin_packages WHERE signer_id=?', (signer_id,))
            writer.execute('DELETE FROM signed_plugin_keys WHERE id=?', (signer_id,))
        self._invalidate()

    @staticmethod
    def _invalidate():
        from infrastructure.ai.prompt_registry import get_prompt_registry
        get_prompt_registry().invalidate_cache()


def uuid_hex():
    from uuid import uuid4
    return uuid4().hex
