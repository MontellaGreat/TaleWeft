from types import SimpleNamespace
import sys
import pytest


def test_local_service_uses_runtime_download_directory_and_optional_packages(tmp_path, monkeypatch):
    from application import paths
    from infrastructure.ai import local_packages
    from infrastructure.ai.local_embedding_service import LocalEmbeddingService
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    for name in ("EMBEDDING_MODEL_PATH", "LOCAL_EMBEDDING_MODEL_PATH"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "models" / "bge-small-zh-v1.5"
    target.mkdir(parents=True)
    captured = {}

    class LocalModel:
        def __init__(self, model_name, **kwargs):
            captured.update(path=model_name, options=kwargs)

        def get_sentence_embedding_dimension(self):
            return 512

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=LocalModel))
    def activate_packages():
        captured["packages_activated"] = True
        return tmp_path / "local-python-packages"
    monkeypatch.setattr(local_packages, "ensure_local_packages_path", activate_packages)
    service = LocalEmbeddingService(use_gpu=False)
    assert captured["path"] == str(target)
    assert captured["options"]["local_files_only"] is True
    assert captured["packages_activated"] is True
    assert service.get_dimension() == 512


def test_missing_default_model_does_not_load_an_unrelated_legacy_models_directory(tmp_path, monkeypatch):
    from application import paths
    from infrastructure.ai.local_embedding_service import LocalEmbeddingService
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "PLOTPILOT_ROOT", tmp_path)
    for name in ("EMBEDDING_MODEL_PATH", "LOCAL_EMBEDDING_MODEL_PATH"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".models").mkdir()
    # The legacy directory belongs to the repository root used by the old code.
    monkeypatch.setattr("infrastructure.ai.local_embedding_service.__file__", str(tmp_path / "infrastructure" / "ai" / "local_embedding_service.py"))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=lambda *args, **kwargs:
                         SimpleNamespace(get_sentence_embedding_dimension=lambda: 512)))
    with pytest.raises(FileNotFoundError):
        LocalEmbeddingService(use_gpu=False)
