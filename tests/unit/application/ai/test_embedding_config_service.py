from application.ai.embedding_config_service import EmbeddingConfigService


def test_embedding_config_default_row_uses_embedding_environment(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "env-embedding-model")
    monkeypatch.setenv("LOCAL_EMBEDDING_MODEL_PATH", "/legacy/local/model")
    monkeypatch.setenv("EMBEDDING_MODEL_PATH", "/current/local/model")

    defaults = EmbeddingConfigService._default_row_values()

    assert defaults["model"] == "env-embedding-model"
    assert defaults["model_path"] == "/legacy/local/model"
    assert defaults["mode"] == "local"


def test_new_config_uses_runtime_local_model_directory(monkeypatch, tmp_path):
    from application import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    for name in ("EMBEDDING_SERVICE", "EMBEDDING_MODEL_PATH", "LOCAL_EMBEDDING_MODEL_PATH"):
        monkeypatch.delenv(name, raising=False)
    defaults = EmbeddingConfigService._default_row_values()
    assert defaults["mode"] == "local"
    assert defaults["model_path"] == str(tmp_path / "models" / "bge-small-zh-v1.5")


def test_explicit_cloud_defaults_keep_environment_configuration(monkeypatch):
    monkeypatch.setenv("EMBEDDING_SERVICE", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "my-embedding")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-only-key")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://embedding.invalid/v1")
    defaults = EmbeddingConfigService._default_row_values()
    assert defaults["mode"] == "openai"
    assert defaults["api_key"] == "test-only-key"
    assert defaults["base_url"] == "https://embedding.invalid/v1"
