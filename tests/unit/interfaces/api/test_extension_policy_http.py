"""Actual signature verification, writer commits, policy rollback and production scans."""
import base64
import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI

from application.audit.services.anti_ai_audit import AntiAIAuditor
from application.audit.services.quality_rule_models import QualityRuleConfig
from infrastructure.ai.quality_rule_store import QualityRuleStore, RuleVersionConflict
from infrastructure.ai.signed_plugins import SignedPluginPackage, SignedPluginStore, PluginRejectedError
from interfaces.api.middleware.session_auth import SessionAuthority, SessionAuthMiddleware
from interfaces.api.v1.core.extension_policy_routes import router
from tests.unit.application.engine.test_persistence_commit_ack import writer

TOKEN = 'extension-policy-session-0123456789abcdef'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


@pytest.fixture
def package():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    data = {'manifest': {'id': 'test-style', 'name': '测试文风包', 'version': '1.0.0', 'signer_id': 'publisher',
        'permissions': ['prompts', 'quality_rules']}, 'payload': {
            'prompts': [{'key': 'outline', 'name': '测试大纲', 'system_prompt': '注意 {{ context }}', 'user_template': '{outline}'}],
            'quality_rules': [{'name': '测试词', 'text': '灯下轻语', 'severity': 'critical'}]}, 'signature': 'A' * 88}
    normalized = SignedPluginPackage.model_validate(data)
    normalized.signature = base64.b64encode(private.sign(normalized.signing_bytes())).decode()
    return public, normalized


def trust(store, public, permissions=None):
    return store.trust(id='publisher', name='测试发布者', public_key=public,
                       permissions=['prompts', 'quality_rules'] if permissions is None else permissions)


@pytest.mark.asyncio
async def test_http_signature_permissions_install_real_scan_and_revoke(writer, package):
    _queue, db = writer
    public, envelope = package
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.add_middleware(SessionAuthMiddleware, authority=SessionAuthority(TOKEN))
    base = '/api/v1/extensions'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        assert (await client.get(base + '/plugins')).status_code == 401
        assert (await client.post(base + '/plugins/verify', headers=AUTH, json=envelope.model_dump())).status_code == 422
        key = await client.post(base + '/plugins/keys', headers=AUTH,
            json={'id': 'publisher', 'name': '测试发布者', 'public_key': public, 'permissions': ['prompts', 'quality_rules']})
        assert key.status_code == 200 and len(key.json()['fingerprint']) == 64
        preview = await client.post(base + '/plugins/verify', headers=AUTH, json=envelope.model_dump())
        assert preview.status_code == 200 and preview.json()['verified']
        wrong = await client.post(base + '/plugins/install', headers=AUTH,
            json={'package': envelope.model_dump(), 'expected_digest': '0' * 64})
        assert wrong.status_code == 422
        installed = await client.post(base + '/plugins/install', headers=AUTH,
            json={'package': envelope.model_dump(), 'expected_digest': preview.json()['digest']})
        assert installed.status_code == 200, installed.text
        # Read a fresh connection, proving a committed prompt, not queue acceptance.
        with sqlite3.connect(db.db_path) as connection:
            assert connection.execute("SELECT name FROM prompt_nodes WHERE node_key='plugin-test-style-outline'").fetchone()[0] == '测试大纲'
        scanned = await client.post(base + '/quality-rules/scan', headers=AUTH, json={'content': '灯下轻语。'})
        assert scanned.json()['metrics']['critical_hits'] == 1
        version = scanned.json()['rule_version']
        snapshot = await client.get(base + '/quality-rules/snapshots/' + version, headers=AUTH)
        assert snapshot.status_code == 200 and snapshot.json()['plugins'][0]['id'] == 'test-style'
        # Both provenance and behavior change when trust is withdrawn.
        assert (await client.delete(base + '/plugins/keys/publisher', headers=AUTH)).status_code == 200
        assert not SignedPluginStore(db).list_packages()
        assert db.fetch_one("SELECT id FROM prompt_nodes WHERE node_key='plugin-test-style-outline'") is None
        scanned = await client.post(base + '/quality-rules/scan', headers=AUTH, json={'content': '灯下轻语。'})
        assert scanned.json()['metrics']['total_hits'] == 0 and scanned.json()['rule_version'] != version
        assert (await client.get(base + '/quality-rules/snapshots/' + version, headers=AUTH)).status_code == 200


@pytest.mark.parametrize('change', ['body', 'manifest', 'signature', 'missing_grant', 'extra_code', 'template_code'])
def test_changed_or_executable_package_cannot_write(writer, package, change):
    _queue, db = writer
    public, original = package
    store = SignedPluginStore(db)
    trust(store, public, ['prompts'] if change == 'missing_grant' else None)
    data = copy.deepcopy(original.model_dump())
    if change == 'body':
        data['payload']['quality_rules'][0]['text'] = 'changed'
    elif change == 'manifest':
        data['manifest']['id'] = 'other'
    elif change == 'signature':
        data['signature'] = base64.b64encode(b'\0' * 64).decode()
    elif change == 'extra_code':
        data['payload']['module'] = 'os.system'
    elif change == 'template_code':
        data['payload']['prompts'][0]['system_prompt'] = '{{ cycler.__init__.__globals__.os.popen("echo executed").read() }}'
    with pytest.raises(ValueError):
        parsed = SignedPluginPackage.model_validate(data)
        store.install(parsed, expected_digest=store.verify(parsed)['digest'])
    assert store.list_packages() == []
    assert db.fetch_one("SELECT id FROM prompt_templates WHERE id='signed-plugin:test-style'") is None


def test_install_rollback_and_uninstall_are_atomic(writer, package):
    _queue, db = writer
    public, envelope = package
    store = SignedPluginStore(db)
    trust(store, public)
    db.execute("INSERT INTO prompt_templates(id,name) VALUES ('unrelated','作者模板')")
    db.execute("INSERT INTO prompt_nodes(id,template_id,node_key,name) VALUES ('unrelated','unrelated','plugin-test-style-outline','作者提示')")
    with pytest.raises(PluginRejectedError, match='未提交'):
        store.install(envelope, expected_digest=store.verify(envelope)['digest'])
    assert store.list_packages() == []
    assert db.fetch_one("SELECT id FROM prompt_templates WHERE id='signed-plugin:test-style'") is None
    assert db.fetch_one("SELECT name FROM prompt_nodes WHERE id='unrelated'")['name'] == '作者提示'
    db.execute("DELETE FROM prompt_nodes WHERE id='unrelated'")
    store.install(envelope, expected_digest=store.verify(envelope)['digest'])
    store.uninstall('test-style')
    assert store.list_packages() == []
    assert db.fetch_one("SELECT name FROM prompt_templates WHERE id='unrelated'")['name'] == '作者模板'


def test_revocation_winning_install_race_rolls_back_all_resources(writer, package, monkeypatch):
    _queue, db = writer
    public, envelope = package
    store = SignedPluginStore(db)
    trust(store, public)
    preview = store.verify(envelope)
    original_keys = store.keys
    def read_then_revoke():
        keys = original_keys()
        store.revoke('publisher')
        return keys
    monkeypatch.setattr(store, 'keys', read_then_revoke)
    with pytest.raises(PluginRejectedError, match='未提交'):
        store.install(envelope, expected_digest=preview['digest'])
    assert store.list_packages() == []
    assert db.fetch_one("SELECT id FROM prompt_templates WHERE id='signed-plugin:test-style'") is None



@pytest.mark.asyncio
async def test_http_rule_publish_rollback_conflict_and_literal_matching(writer):
    _queue, db = writer
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.add_middleware(SessionAuthMiddleware, authority=SessionAuthority(TOKEN))
    base = '/api/v1/extensions/quality-rules'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        before = (await client.get(base, headers=AUTH)).json()
        config = {'disabled_builtin': ['嘴角微表情'], 'rules': [{'name': '字面符号', 'text': 'a+b', 'severity': 'critical'}]}
        posted = await client.post(base, headers=AUTH, json={'config': config, 'note': '测试版本', 'expected_version': before['current']['id']})
        assert posted.status_code == 200
        first = posted.json()['id']
        assert QualityRuleStore(db).current()['id'] == first
        conflict = await client.post(base, headers=AUTH, json={'config': config, 'note': '过时请求', 'expected_version': before['current']['id']})
        assert conflict.status_code == 409
        scanned = (await client.post(base + '/scan', headers=AUTH, json={'content': '嘴角上扬。a+b。aaab。'})).json()
        assert [h['text'] for h in scanned['hits']] == ['a+b']
        frozen = scanned['rule_version']
        rolled = await client.post(base + '/versions/builtin-v1/rollback', headers=AUTH, json={'expected_version': first})
        assert rolled.status_code == 200 and rolled.json()['id'] not in {first, 'builtin-v1'}
        scanned = (await client.post(base + '/scan', headers=AUTH, json={'content': '嘴角上扬。a+b。aaab。'})).json()
        assert [h['pattern'] for h in scanned['hits']] == ['嘴角微表情']
        assert scanned['rule_version'] != frozen
        old = (await client.get(base + '/snapshots/' + frozen, headers=AUTH)).json()
        assert old['policy_version'] == first


def test_concurrent_rule_publication_commits_only_one_version(writer):
    _queue, db = writer
    store = QualityRuleStore(db)
    before = store.current()['id']
    def publish(index):
        try:
            return store.publish(QualityRuleConfig(), expected_version=before, note=f'并发 {index}')['id']
        except RuleVersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_versions()) == 2


def test_default_auditor_and_dag_use_actual_persisted_policy(writer):
    _queue, db = writer
    from application.audit.services import anti_ai_audit
    from application.engine.dag.anti_ai_scan import scan_chapter
    store = QualityRuleStore(db)
    saved = store.publish(QualityRuleConfig(rules=[{'name': '生产规则', 'text': '静灯', 'severity': 'critical'}]),
                          expected_version=store.current()['id'], note='生产策略')
    report = anti_ai_audit.get_anti_ai_auditor().scan_chapter('chapter', '静灯')
    result = scan_chapter('静灯', {'chapter_number': 1})
    assert report.metrics.critical_hits == 1
    assert result.outputs['rule_version'] == report.rule_version
    assert store.snapshot(report.rule_version)['policy_version'] == saved['id']


def test_workflow_scanner_reloads_rules_after_policy_change(writer):
    _queue, db = writer
    from application.audit.services.cliche_scanner import ClicheScanner
    store = QualityRuleStore(db)
    scanner = ClicheScanner(rule_provider=store.effective)
    assert scanner.scan_cliches('静灯') == []
    store.publish(QualityRuleConfig(rules=[{'name': '新规则', 'text': '静灯'}]),
                  expected_version=store.current()['id'], note='工作流规则')
    assert scanner.scan_cliches('静灯')[0].pattern == '新规则'
    assert store.snapshot(scanner.rule_version)['policy_version'] == store.current()['id']


def test_package_author_tool_produces_verified_signature(writer, tmp_path):
    _queue, db = writer
    script = Path('scripts/sign_data_plugin.py').resolve()
    keydir = tmp_path / 'author-keys'
    generated = subprocess.run([sys.executable, str(script), 'keygen', '--directory', str(keydir)],
                               text=True, capture_output=True, check=True)
    assert 'Fingerprint:' in generated.stdout
    raw = tmp_path / 'unsigned.json'
    output = tmp_path / 'signed.json'
    raw.write_text(json.dumps({'manifest': {'id': 'author-style', 'name': 'Author package', 'version': '1.0.0',
        'signer_id': 'publisher', 'permissions': ['quality_rules']},
        'payload': {'quality_rules': [{'name': 'Literal rule', 'text': 'literal'}]} }), encoding='utf-8')
    signed = subprocess.run([sys.executable, str(script), 'sign', '--input', str(raw),
        '--private-key', str(keydir / 'publisher.private.key'), '--output', str(output)],
        text=True, capture_output=True, check=True)
    assert 'Content hash:' in signed.stdout
    store = SignedPluginStore(db)
    trust(store, (keydir / 'publisher.public.txt').read_text(encoding='ascii').strip())
    package = SignedPluginPackage.model_validate(json.loads(output.read_text(encoding='utf-8')))
    verified = store.verify(package)
    store.install(package, expected_digest=verified['digest'])
    assert AntiAIAuditor(rule_provider=QualityRuleStore(db).effective).scan_chapter('c', 'literal').metrics.total_hits == 1
    repeated = subprocess.run([sys.executable, str(script), 'keygen', '--directory', str(keydir)], text=True, capture_output=True)
    assert repeated.returncode != 0
