"""The managed local model is ready only after verified, atomic download."""
import hashlib
import json
import threading

import httpx
import pytest


def model_files():
    return {
        "config.json": b'{"model_type":"bert","hidden_size":512}',
        "modules.json": b'[{"idx":0,"name":"0","path":"","type":"sentence_transformers.models.Transformer"},{"idx":1,"name":"1","path":"1_Pooling","type":"sentence_transformers.models.Pooling"}]',
        "1_Pooling/config.json": b'{"word_embedding_dimension":512,"pooling_mode_cls_token":true}',
        "sentence_bert_config.json": b'{"max_seq_length":512}',
        "config_sentence_transformers.json": b'{}',
        "tokenizer.json": b'{"model":{"type":"WordPiece"}}',
        "tokenizer_config.json": b'{"tokenizer_class":"BertTokenizer"}',
        "special_tokens_map.json": b'{"unk_token":"[UNK]"}',
        "vocab.txt": b'[PAD]\n[UNK]\n[CLS]\n[SEP]\n',
        "model.safetensors": b'\x08\x00\x00\x00\x00\x00\x00\x00{}\x00\x00weights',
    }


def transport_for(files, *, modelscope_unavailable=False, corrupt_modelscope=False):
    def handle(request):
        host = request.url.host
        if "/repo/files" in request.url.path:
            if modelscope_unavailable:
                return httpx.Response(503)
            return httpx.Response(200, json={"Code": 200, "Data": {"Files": [
                {"Path": name, "Type": "blob", "Size": len(data), "Sha256": hashlib.sha256(data).hexdigest()}
                for name, data in files.items()
            ]}})
        if "/tree/" in request.url.path:
            return httpx.Response(200, json=[
                {"path": name, "type": "file", "size": len(data), "lfs": {"oid": hashlib.sha256(data).hexdigest()}}
                for name, data in files.items()
            ])
        name = request.url.path.split("/resolve/", 1)[-1].split("/", 1)[-1]
        data = files[name]
        if corrupt_modelscope and host == "modelscope.cn" and name == "model.safetensors":
            data = b"corrupt" + data[7:]
        # A range probe must not download the whole weights.
        if request.headers.get("range"):
            return httpx.Response(206, content=data[:1], headers={"Content-Range": f"bytes 0-0/{len(data)}"})
        return httpx.Response(200, content=data)
    return httpx.MockTransport(handle)


@pytest.mark.parametrize("failure", ["unavailable", "checksum"])
def test_download_falls_back_to_reachable_verified_domestic_source(tmp_path, failure):
    from infrastructure.ai.embedding_download import EmbeddingModelDownloader, get_model_status
    files = model_files()
    with httpx.Client(transport=transport_for(files, modelscope_unavailable=failure == "unavailable", corrupt_modelscope=failure == "checksum")) as client:
        events = []
        result = EmbeddingModelDownloader(tmp_path / "model", client=client).download(events.append)
    assert result["model_ready"] is True
    assert result["download_source"] == "HF 镜像"
    assert (tmp_path / "model" / "model.safetensors").read_bytes() == files["model.safetensors"]
    assert get_model_status(tmp_path / "model")["model_ready"] is True
    assert any(event["type"] == "warn" for event in events)


def test_empty_directory_and_missing_weight_never_count_as_ready(tmp_path):
    from infrastructure.ai.embedding_download import get_model_status
    assert get_model_status(tmp_path)["model_ready"] is False
    (tmp_path / ".plotpilot-model.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    assert get_model_status(tmp_path)["model_ready"] is False


def test_modified_weight_is_no_longer_ready(tmp_path):
    from infrastructure.ai.embedding_download import EmbeddingModelDownloader, get_model_status
    with httpx.Client(transport=transport_for(model_files())) as client:
        EmbeddingModelDownloader(tmp_path / "model", client=client).download()
    weight = tmp_path / "model/model.safetensors"
    weight.write_bytes(b"!" + weight.read_bytes()[1:])
    assert get_model_status(tmp_path / "model")["model_ready"] is False


def test_cancel_cleans_staging_without_publishing_partial_model(tmp_path):
    from infrastructure.ai.embedding_download import EmbeddingModelDownloader, DownloadCancelled
    cancel = threading.Event()
    def progress(event):
        if event.get("phase") == "model_download" and event.get("downloaded_bytes", 0) > 0:
            cancel.set()
    with httpx.Client(transport=transport_for(model_files())) as client:
        with pytest.raises(DownloadCancelled):
            EmbeddingModelDownloader(tmp_path / "model", client=client).download(progress, cancel)
    assert not (tmp_path / "model").exists()
    assert list(tmp_path.iterdir()) == []


def test_metadata_missing_real_weight_is_rejected_without_ready_marker(tmp_path):
    from infrastructure.ai.embedding_download import EmbeddingModelDownloader, ModelDownloadError
    files = model_files()
    del files["model.safetensors"]
    with httpx.Client(transport=transport_for(files)) as client:
        with pytest.raises(ModelDownloadError):
            EmbeddingModelDownloader(tmp_path / "model", client=client).download()
    assert not (tmp_path / "model/.plotpilot-model.json").exists()


def test_failed_publish_and_rollback_keep_original_model_backup(monkeypatch, tmp_path):
    from pathlib import Path
    from infrastructure.ai.embedding_download import EmbeddingModelDownloader, ModelDownloadError
    target = tmp_path / "model"
    target.mkdir()
    (target / "original.txt").write_text("original user model", encoding="utf-8")
    rename = Path.rename
    def fail_publish_and_restore(path, destination):
        if Path(destination) == target and path != target:
            raise OSError("simulated directory lock")
        return rename(path, destination)
    monkeypatch.setattr(Path, "rename", fail_publish_and_restore)
    with httpx.Client(transport=transport_for(model_files())) as client:
        with pytest.raises(ModelDownloadError):
            EmbeddingModelDownloader(target, client=client).download()
    backups = list(tmp_path.glob(".model-previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "original.txt").read_text(encoding="utf-8") == "original user model"
