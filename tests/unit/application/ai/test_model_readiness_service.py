"""Readiness checks configuration and local artifacts without starting providers."""
import asyncio
import json
import sqlite3
import struct

import httpx
import pytest
from fastapi import FastAPI

from application.ai.embedding_config_service import EmbeddingConfigModel, EmbeddingConfigService
from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile


class ConfiguredLLM(LLMControlService):
    def __init__(self, profiles, active="active", endpoint_mode="unified", runtime=None):
        self.config = LLMControlConfig(profiles=profiles, active_profile_id=active, endpoint_mode=endpoint_mode)
        self.runtime = runtime

    def get_config_readonly(self):
        return self.config

    def get_runtime_summary(self, config=None):
        return self.runtime if self.runtime is not None else super().get_runtime_summary(config)


class ConfiguredEmbedding:
    def __init__(self, **kwargs):
        self.config = EmbeddingConfigModel(**kwargs)

    def get_config_readonly(self):
        return self.config


def profile(**kwargs):
    return LLMProfile(**{"id": "active", "name": "Active", "model": "real-model",
                         "api_key": "llm-private-key", "base_url": "https://llm.invalid/v1", **kwargs})


def cloud(**kwargs):
    return ConfiguredEmbedding(**{"mode": "openai", "model": "embedding-model",
                                  "base_url": "https://embedding.invalid/v1",
                                  "api_key": "embedding-private-key", **kwargs})


def service(llm=None, embedding=None, **kwargs):
    from application.ai.model_readiness_service import ModelReadinessService
    return ModelReadinessService(llm_control_service=llm or ConfiguredLLM([profile()]),
                                 embedding_config_service=embedding or cloud(), **kwargs)


def test_only_effective_active_profile_can_make_llm_ready():
    result = service(llm=ConfiguredLLM([profile(api_key="", model=""), profile(id="inactive")])).get_readiness()
    assert result["ready"] is False
    assert result["llm"]["ready"] is False
    assert result["embedding"]["ready"] is True


def test_unified_ignores_incomplete_inactive_profiles_and_allows_keyless_local_endpoint():
    llm = ConfiguredLLM([profile(auth_mode="none", api_key="", base_url="http://localhost:11434/v1"),
                         profile(id="unused", model="", api_key="")])
    assert service(llm=llm).get_readiness() == {
        "ready": True, "llm": {"ready": True, "reasons": []},
        "embedding": {"ready": True, "reasons": [], "mode": "openai"},
    }


@pytest.mark.parametrize("runtime", [
    {"source": "profile", "status": "configured", "using_mock": True},
    {"source": "unavailable", "status": "unavailable", "using_mock": False},
])
def test_runtime_mock_or_unavailable_cannot_pass_configuration_gate(runtime):
    result = service(llm=ConfiguredLLM([profile()], runtime=runtime)).get_readiness()
    assert result["llm"]["ready"] is False
    assert result["llm"]["reasons"]


@pytest.mark.parametrize("field", ["model", "api_key", "base_url"])
def test_cloud_embedding_requires_each_connection_field(field):
    result = service(embedding=cloud(**{field: "  "})).get_readiness()
    assert result["embedding"]["ready"] is False
    assert result["embedding"]["reasons"]


def test_independent_roles_must_exist_and_be_complete():
    llm = ConfiguredLLM([profile(), profile(id="cheap", name="经济模型", model="")], endpoint_mode="independent")
    result = service(llm=llm).get_readiness()
    assert result["llm"]["ready"] is False
    assert any("经济" in reason for reason in result["llm"]["reasons"])
    assert any("知识图谱" in reason for reason in result["llm"]["reasons"])
    llm.config.profiles = [profile(), profile(id="cheap", name="经济模型"), profile(id="kg", name="知识图谱模型")]
    assert service(llm=llm).get_readiness()["ready"] is True


def test_invalid_urls_and_credential_placeholders_are_not_ready():
    assert service(llm=ConfiguredLLM([profile(base_url="not-a-url")])).get_readiness()["llm"]["ready"] is False
    assert service(embedding=cloud(api_key="********")).get_readiness()["embedding"]["ready"] is False


def test_official_preset_url_is_checked_after_resolution():
    assert service(llm=ConfiguredLLM([profile(base_url="", preset_key="openai-official")])).get_readiness()["llm"]["ready"] is True
    assert service(llm=ConfiguredLLM([profile(base_url="")])).get_readiness()["llm"]["ready"] is False


def test_config_errors_are_unready_without_returning_secret_exception_details():
    class BrokenEmbedding:
        def get_config_readonly(self):
            raise RuntimeError("https://secret-key@provider.invalid credential=private-key")
    result = service(embedding=BrokenEmbedding()).get_readiness()
    assert result["ready"] is False
    assert result["embedding"]["reasons"]
    assert "private-key" not in json.dumps(result)
    assert "secret-key" not in json.dumps(result)


def test_cloud_embedding_checks_environment_fallback_used_by_runtime(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "environment-private-key")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://embedding-env.invalid/v1")
    result = service(embedding=cloud(api_key="", base_url="")).get_readiness()
    assert result["embedding"]["ready"] is True
    assert "environment-private-key" not in json.dumps(result)


def write_local_model(path):
    path.mkdir(exist_ok=True)
    (path / "config.json").write_text(json.dumps({"model_type": "bert", "hidden_size": 1}), encoding="utf-8")
    (path / "vocab.txt").write_text("[UNK]\n[CLS]\n[SEP]\n", encoding="utf-8")
    header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    (path / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\x00" * 4)


def test_local_requires_dependencies_and_complete_model_without_loading_torch(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch.") or name == "sentence_transformers":
            raise AssertionError("readiness imported heavy dependency")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    embedding = ConfiguredEmbedding(mode="local", model_path=str(tmp_path))
    ready = service(embedding=embedding, local_dependencies_status=lambda: {"all_installed": True})
    assert ready.get_readiness()["embedding"]["ready"] is False  # empty directory
    write_local_model(tmp_path)
    assert ready.get_readiness()["embedding"]["ready"] is True
    unavailable = service(embedding=embedding, local_dependencies_status=lambda: {"all_installed": False})
    assert unavailable.get_readiness()["embedding"]["ready"] is False
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(weights.read_bytes()[:-1])
    assert ready.get_readiness()["embedding"]["ready"] is False


def test_missing_custom_path_cannot_silently_use_downloaded_default(tmp_path):
    result = service(embedding=ConfiguredEmbedding(mode="local", model_path=str(tmp_path / "missing")),
                     local_dependencies_status=lambda: {"all_installed": True}).get_readiness()
    assert result["embedding"]["ready"] is False


def test_pending_dependency_validation_keeps_local_unready_with_a_specific_reason(tmp_path):
    write_local_model(tmp_path)
    result = service(embedding=ConfiguredEmbedding(mode="local", model_path=str(tmp_path)),
                     local_dependencies_status=lambda: {"all_installed": False, "checking": True}).get_readiness()
    assert result["embedding"]["ready"] is False
    assert any("验证" in reason for reason in result["embedding"]["reasons"])


def test_http_contract_hides_credentials_and_exception_details():
    from interfaces.api.v1.core import model_readiness_routes as routes
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes.get_model_readiness_service] = lambda: service()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/v1/settings/model-readiness")
    response = asyncio.run(run())
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert "private-key" not in response.text


class ReadOnlyDatabase:
    """A real SQLite fixture that permits SELECT only after setup."""
    def __init__(self, path):
        self.db_path = str(path)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE llm_profiles(id TEXT, name TEXT, preset_key TEXT, protocol TEXT, base_url TEXT,
                api_key TEXT, model TEXT, temperature REAL, max_tokens INTEGER, timeout_seconds INTEGER,
                extra_headers TEXT, extra_query TEXT, extra_body TEXT, notes TEXT,
                use_legacy_chat_completions INTEGER, sort_order INTEGER, created_at TEXT);
            CREATE TABLE llm_config_meta(key TEXT, value TEXT);
            CREATE TABLE embedding_config(id TEXT, mode TEXT, api_key TEXT, base_url TEXT, model TEXT,
                use_gpu INTEGER, model_path TEXT, created_at TEXT, updated_at TEXT);
        """)

    def execute(self, sql, parameters=()):
        return self.connection.execute(sql, parameters)

    def get_connection(self):
        return self.connection

    def fetch_all(self, sql, parameters=()):
        return [dict(row) for row in self.execute(sql, parameters).fetchall()]

    def fetch_one(self, sql, parameters=()):
        row = self.execute(sql, parameters).fetchone()
        return dict(row) if row else None


def test_readonly_config_reads_do_not_initialize_or_migrate_credentials(tmp_path, monkeypatch):
    db = ReadOnlyDatabase(tmp_path / "readiness.sqlite")
    llm = LLMControlService()
    monkeypatch.setattr(llm, "_db", lambda: db)
    embedding = EmbeddingConfigService(db)
    before = db.connection.total_changes
    llm.get_config_readonly()
    assert embedding.get_config_readonly().mode == "local"
    assert db.connection.total_changes == before
    assert db.execute("SELECT count(*) FROM llm_profiles").fetchone()[0] == 0
    db.execute("INSERT INTO llm_profiles VALUES ('active','Active','openai-official','openai','','llm-legacy-private-key','real-model',0.7,1000,300,'{}','{}','{}','',0,0,'')")
    before = db.connection.total_changes
    assert llm.get_config_readonly().profiles[0].api_key == "llm-legacy-private-key"
    assert db.connection.total_changes == before
    db.execute("INSERT INTO embedding_config VALUES ('default','openai','legacy-private-key','https://embedding.invalid/v1','model',0,'','','')")
    before = db.connection.total_changes
    assert embedding.get_config_readonly().api_key == "legacy-private-key"
    assert db.connection.total_changes == before
    db.connection.close()


@pytest.mark.parametrize("field", [None, "api_key", "base_url", "model"])
def test_legacy_empty_cloud_placeholder_uses_local_but_preserves_user_configuration(tmp_path, monkeypatch, field):
    db = ReadOnlyDatabase(tmp_path / "legacy.sqlite")
    monkeypatch.delenv("EMBEDDING_SERVICE", raising=False)
    fields = {"api_key": "", "base_url": "", "model": ""}
    if field:
        fields[field] = "configured-value"
    db.execute("INSERT INTO embedding_config VALUES ('default','openai',?,?,?,1,'','','')",
               (fields["api_key"], fields["base_url"], fields["model"]))
    before = db.connection.total_changes
    result = EmbeddingConfigService(db).get_config_readonly()
    assert result.mode == ("openai" if field else "local")
    if field:
        assert getattr(result, field) == "configured-value"
    else:
        assert result.model_path.endswith("bge-small-zh-v1.5")
    assert db.connection.total_changes == before
    db.connection.close()


def test_explicit_environment_cloud_mode_is_preserved_even_before_fields_are_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_SERVICE", "openai")
    for name in ("EMBEDDING_API_KEY", "OPENAI_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    db = ReadOnlyDatabase(tmp_path / "explicit.sqlite")
    config = EmbeddingConfigService(db).get_config_readonly()
    assert config.mode == "openai"
    db.connection.close()


def test_environment_cloud_key_is_protected_before_initial_insert(tmp_path, monkeypatch):
    from application.ai import embedding_config_service as module
    monkeypatch.setenv("EMBEDDING_SERVICE", "openai")
    monkeypatch.setenv("EMBEDDING_API_KEY", "environment-private-key")
    monkeypatch.setattr(module, "credentials_for_database", lambda _db: type("Vault", (), {
        "store": lambda self, value: "ppcred:v1:" + "a" * 32,
    })())
    db = ReadOnlyDatabase(tmp_path / "protected.sqlite")
    EmbeddingConfigService(db)._ensure_row()
    assert db.execute("SELECT api_key FROM embedding_config").fetchone()[0] == "ppcred:v1:" + "a" * 32
    db.connection.close()
