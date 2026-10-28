"""Download the open BGE model through public ModelScope/Hugging Face HTTP APIs.

No model SDK or torch import is needed. Files are verified in a sibling staging
directory and published only after the complete model has passed validation.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable

import httpx

MODEL_NAME = "bge-small-zh-v1.5"
MANIFEST_NAME = ".plotpilot-model.json"
REQUIRED_FILES = (
    "config.json", "modules.json", "1_Pooling/config.json",
    "sentence_bert_config.json", "config_sentence_transformers.json",
    "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
    "vocab.txt", "model.safetensors",
)
MODEL_SOURCES = (
    {"name": "ModelScope 魔搭", "kind": "modelscope", "metadata": "https://modelscope.cn/api/v1/models/AI-ModelScope/bge-small-zh-v1.5/repo/files?Revision=master&Recursive=true", "base": "https://modelscope.cn/models/AI-ModelScope/bge-small-zh-v1.5/resolve/master/"},
    {"name": "HF 镜像", "kind": "huggingface", "metadata": "https://hf-mirror.com/api/models/BAAI/bge-small-zh-v1.5/tree/main?recursive=true", "base": "https://hf-mirror.com/BAAI/bge-small-zh-v1.5/resolve/main/"},
    {"name": "Hugging Face 官方", "kind": "huggingface", "metadata": "https://huggingface.co/api/models/BAAI/bge-small-zh-v1.5/tree/main?recursive=true", "base": "https://huggingface.co/BAAI/bge-small-zh-v1.5/resolve/main/"},
)
_verified_cache: dict[tuple, dict] = {}
_cache_lock = threading.Lock()


class ModelDownloadError(RuntimeError):
    pass


class DownloadCancelled(ModelDownloadError):
    pass


def get_local_model_path() -> Path:
    from application.paths import resolve_runtime_data_path
    return resolve_runtime_data_path(Path("models") / MODEL_NAME)


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_model_status(path: Path | str | None = None) -> dict:
    """Validate the managed model without importing torch; cache unchanged files."""
    target = Path(path) if path is not None else get_local_model_path()
    result = {"model_ready": False, "model_path": str(target), "download_source": None, "reason": "模型尚未完整下载"}
    try:
        manifest_path = target / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        if not isinstance(files, dict) or not set(REQUIRED_FILES).issubset(files):
            return result
        signature = [str(target.resolve()), manifest_path.stat().st_mtime_ns]
        for name, entry in files.items():
            if not _safe_name(name) or not isinstance(entry, dict):
                return result
            file_path = target / name
            if file_path.is_symlink():
                return result
            stat = file_path.stat()
            if not file_path.is_file() or stat.st_size <= 0 or stat.st_size != entry.get("size"):
                return result
            signature.append((name, stat.st_size, stat.st_mtime_ns))
        key = tuple(signature)
        with _cache_lock:
            cached = _verified_cache.get(key)
        if cached:
            return dict(cached)
        for name, entry in files.items():
            if _sha256(target / name) != entry.get("sha256"):
                result["reason"] = f"模型文件校验失败: {name}"
                return result
        result.update(model_ready=True, download_source=manifest.get("source"), reason="")
        with _cache_lock:
            if len(_verified_cache) >= 16:
                _verified_cache.clear()
            _verified_cache[key] = dict(result)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return result


class EmbeddingModelDownloader:
    def __init__(self, target: Path | str | None = None, *, client: httpx.Client | None = None):
        self.target = Path(target) if target is not None else get_local_model_path()
        self.client = client

    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise DownloadCancelled("模型下载已取消")

    def _metadata(self, client: httpx.Client, source: dict) -> dict:
        response = client.get(source["metadata"])
        response.raise_for_status()
        data = response.json()
        if source["kind"] == "modelscope":
            rows = data["Data"]["Files"]
            entries = {row["Path"]: {"size": row["Size"], "sha256": row.get("Sha256")} for row in rows if row.get("Type") == "blob"}
        else:
            entries = {row["path"]: {"size": row["size"], "sha256": row.get("lfs", {}).get("oid"), "git_sha1": row.get("oid")} for row in data if row.get("type") == "file"}
        selected = {}
        for name in REQUIRED_FILES:
            entry = entries.get(name)
            if not entry or not isinstance(entry["size"], int) or entry["size"] <= 0:
                raise ModelDownloadError(f"源缺少完整模型文件: {name}")
            if not entry.get("sha256") and not entry.get("git_sha1"):
                raise ModelDownloadError(f"源缺少校验信息: {name}")
            selected[name] = entry
        return selected

    def probe_source(self, client: httpx.Client, source: dict) -> dict:
        """Probe metadata, a real config, and the weight endpoint before selecting."""
        files = self._metadata(client, source)
        response = client.get(source["base"] + "config.json")
        response.raise_for_status()
        if not isinstance(response.json(), dict) or "model_type" not in response.json():
            raise ModelDownloadError("模型配置响应无效")
        # Some CDNs ignore Range. Streaming and closing immediately keeps this a
        # small probe even when the server offers the full 96 MB file.
        with client.stream("GET", source["base"] + "model.safetensors", headers={"Range": "bytes=0-0"}) as response:
            response.raise_for_status()
            if not next(response.iter_bytes(chunk_size=1), b""):
                raise ModelDownloadError("模型权重不可读取")
        return files

    def download(self, progress: Callable[[dict], None] | None = None, cancel: threading.Event | None = None) -> dict:
        progress = progress or (lambda event: None)
        cancel = cancel or threading.Event()
        existing = get_model_status(self.target)
        if existing["model_ready"]:
            return existing
        self._check_cancel(cancel)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        owned_client = self.client is None
        client = self.client or httpx.Client(timeout=httpx.Timeout(30, connect=8), follow_redirects=True)
        errors = []
        try:
            for source in MODEL_SOURCES:
                self._check_cancel(cancel)
                progress({"type": "info", "phase": "model_probe", "message": f"检测模型源: {source['name']}", "download_source": source["name"]})
                try:
                    files = self.probe_source(client, source)
                    self._check_cancel(cancel)
                    total = sum(entry["size"] for entry in files.values())
                    if shutil.disk_usage(self.target.parent).free < total + 32 * 1024 * 1024:
                        raise ModelDownloadError("模型目录剩余空间不足，请至少预留 150 MB")
                    with tempfile.TemporaryDirectory(prefix=f".{self.target.name}-", dir=self.target.parent) as temporary:
                        staging = Path(temporary)
                        manifest_files = {}
                        downloaded = 0
                        last_progress = 0.0
                        for name, entry in files.items():
                            self._check_cancel(cancel)
                            file_path = staging / name
                            file_path.parent.mkdir(parents=True, exist_ok=True)
                            digest = hashlib.sha256()
                            git_digest = hashlib.sha1(f"blob {entry['size']}\0".encode())
                            size = 0
                            with client.stream("GET", source["base"] + name) as response:
                                response.raise_for_status()
                                with file_path.open("wb") as output:
                                    for chunk in response.iter_bytes(chunk_size=256 * 1024):
                                        self._check_cancel(cancel)
                                        size += len(chunk)
                                        if size > entry["size"]:
                                            raise ModelDownloadError(f"文件大小超出元数据: {name}")
                                        output.write(chunk)
                                        digest.update(chunk)
                                        git_digest.update(chunk)
                                        downloaded += len(chunk)
                                        now = time.monotonic()
                                        if now - last_progress >= 0.25 or downloaded == total:
                                            progress({"type": "progress", "phase": "model_download", "message": f"下载模型 [{source['name']}]: {name}", "percent": int(downloaded * 100 / total), "downloaded_bytes": downloaded, "total_bytes": total, "download_source": source["name"]})
                                            last_progress = now
                            self._check_cancel(cancel)
                            checksum = digest.hexdigest()
                            if size != entry["size"] or (entry.get("sha256") and checksum != entry["sha256"]) or (not entry.get("sha256") and git_digest.hexdigest() != entry.get("git_sha1")):
                                raise ModelDownloadError(f"文件完整性校验失败: {name}")
                            if name.endswith(".json"):
                                json.loads(file_path.read_text(encoding="utf-8"))
                            manifest_files[name] = {"size": size, "sha256": checksum}
                        manifest = {"format": 1, "model": MODEL_NAME, "source": source["name"], "files": manifest_files}
                        (staging / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                        self._check_cancel(cancel)
                        backup = self.target.with_name(f".{self.target.name}-previous-{uuid.uuid4().hex}")
                        if self.target.exists():
                            self.target.rename(backup)
                        try:
                            staging.rename(self.target)
                        except BaseException:
                            if backup.exists():
                                try:
                                    backup.rename(self.target)
                                except OSError as exc:
                                    raise ModelDownloadError(f"发布模型失败，旧模型已安全保留，请恢复此目录: {backup}") from exc
                            raise
                        else:
                            if backup.exists() and backup.resolve().parent == self.target.resolve().parent:
                                shutil.rmtree(backup)
                    status = get_model_status(self.target)
                    if not status["model_ready"]:
                        raise ModelDownloadError("模型下载后的完整性验证失败")
                    return status
                except DownloadCancelled:
                    raise
                except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, ModelDownloadError) as exc:
                    errors.append(f"{source['name']}: {exc}")
                    progress({"type": "warn", "phase": "model_download", "message": f"模型源 {source['name']} 不可用，尝试下一来源: {exc}"})
            raise ModelDownloadError("所有模型源均不可用，请检查网络后重试。" + "；".join(errors))
        finally:
            if owned_client:
                client.close()
