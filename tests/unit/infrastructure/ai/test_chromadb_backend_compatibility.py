"""Persisted indexes survive optional FAISS installation and cold restarts."""
import asyncio
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from infrastructure.ai import chromadb_vector_store as module


@pytest.fixture
def faiss_runtime(monkeypatch):
    """Use the already verified runtime packages, without installing or writing."""
    project = Path(__file__).resolve().parents[4]
    target = project / "out/model-readiness-tests/runtime-live/local-python-packages"
    if not (target / "faiss").is_dir():
        return pytest.importorskip("faiss"), None
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.syspath_prepend(str(target))
    faiss = importlib.import_module("faiss")
    return faiss, target


def populate(store, vector_id="first", vector=None):
    async def run():
        await store.create_collection("documents", dimension=3)
        await store.insert("documents", vector_id, vector or [1.0, 0.0, 0.0], {"text": vector_id})
    asyncio.run(run())


def matches(store, vector=None):
    return asyncio.run(store.search("documents", vector or [1.0, 0.0, 0.0], 2))


def test_portable_json_still_round_trips_without_faiss(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_get_vector_index_backend", lambda: module._SimpleVectorIndexBackend)
    store = module.ChromaDBVectorStore(str(tmp_path))
    populate(store)
    reopened = module.ChromaDBVectorStore(str(tmp_path))
    assert matches(reopened) == [{"id": "first", "score": 1.0, "payload": {"text": "first"}}]


def test_portable_json_index_loads_with_real_faiss_and_converts_on_save(tmp_path, monkeypatch, faiss_runtime):
    faiss, _target = faiss_runtime
    with monkeypatch.context() as absent:
        absent.setattr(module, "_get_vector_index_backend", lambda: module._SimpleVectorIndexBackend)
        portable = module.ChromaDBVectorStore(str(tmp_path))
        populate(portable)
    index_path = tmp_path / "documents/index.faiss"
    assert json.loads(index_path.read_text(encoding="utf-8"))["vectors"] == [[1.0, 0.0, 0.0]]

    store = module.ChromaDBVectorStore(str(tmp_path))
    assert matches(store)[0] == {"id": "first", "score": 1.0, "payload": {"text": "first"}}
    populate(store, "second", [0.0, 1.0, 0.0])
    native = faiss.read_index(str(index_path))
    assert native.ntotal == 2
    assert native.reconstruct(0).tolist() == [1.0, 0.0, 0.0]
    assert native.reconstruct(1).tolist() == [0.0, 1.0, 0.0]


def test_simple_index_already_in_memory_can_save_after_faiss_becomes_available(tmp_path, monkeypatch, faiss_runtime):
    with monkeypatch.context() as absent:
        absent.setattr(module, "_get_vector_index_backend", lambda: module._SimpleVectorIndexBackend)
        store = module.ChromaDBVectorStore(str(tmp_path))
        populate(store)
    populate(store, "second", [0.0, 1.0, 0.0])
    assert {item["id"] for item in matches(store)} == {"first", "second"}
    reopened = module.ChromaDBVectorStore(str(tmp_path))
    assert {item["id"] for item in matches(reopened)} == {"first", "second"}


def test_cold_restart_mounts_verified_packages_before_loading_existing_binary(tmp_path, faiss_runtime):
    _faiss, target = faiss_runtime
    if target is None:
        pytest.skip("Cold restart requires verified optional packages in a separate runtime directory")
    store = module.ChromaDBVectorStore(str(tmp_path))
    populate(store)
    original_index = (tmp_path / "documents/index.faiss").read_bytes()
    original_metadata = (tmp_path / "documents/metadata.json").read_bytes()
    script = """
import asyncio, json, sys
from pathlib import Path
from application import paths
runtime_data = Path(sys.argv[1])
paths.DATA_DIR = runtime_data
assert str(runtime_data / 'local-python-packages') not in sys.path
from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore
store = ChromaDBVectorStore(sys.argv[2])
results = asyncio.run(store.search('documents', [1.0, 0.0, 0.0], 1))
print(json.dumps(results))
"""
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(target.parent), str(tmp_path)],
                               cwd=Path(__file__).resolve().parents[4], capture_output=True, text=True,
                               timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)[0]["id"] == "first"
    assert (tmp_path / "documents/index.faiss").read_bytes() == original_index
    assert (tmp_path / "documents/metadata.json").read_bytes() == original_metadata


def test_binary_index_without_faiss_reports_requirement_and_preserves_all_files(tmp_path, monkeypatch, faiss_runtime):
    store = module.ChromaDBVectorStore(str(tmp_path))
    populate(store)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(module, "_get_vector_index_backend", lambda: module._SimpleVectorIndexBackend)
    with pytest.raises(RuntimeError, match="FAISS.*(?:依赖|扩展)"):
        module.ChromaDBVectorStore(str(tmp_path))
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
