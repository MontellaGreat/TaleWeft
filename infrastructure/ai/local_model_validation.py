"""Inspect local Transformers artifacts without importing or loading a model.

Managed downloads use their size/hash manifest. User supplied models are checked
against the open safetensors format or PyTorch ZIP container and shard indexes.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import zipfile


def resolve_local_model_path(value: str = "") -> Path:
    from infrastructure.ai.embedding_download import get_local_model_path
    from application.paths import PLOTPILOT_ROOT
    if not value.strip():
        return get_local_model_path()
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    resolved = PLOTPILOT_ROOT / candidate
    if resolved.exists():
        return resolved
    legacy = PLOTPILOT_ROOT / ".models" / candidate.name
    return legacy if legacy.exists() else resolved


def _json(path: Path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("oversized metadata")
    return json.loads(path.read_text(encoding="utf-8"))


def _weight_complete(path: Path) -> bool:
    if path.suffix == ".safetensors":
        with path.open("rb") as stream:
            length = struct.unpack("<Q", stream.read(8))[0]
            if not 2 <= length <= 16 * 1024 * 1024:
                return False
            header = json.loads(stream.read(length))
        sizes = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4,
                 "I16": 2, "U8": 1, "I8": 1, "BOOL": 1, "F8_E4M3": 1, "F8_E5M2": 1}
        offsets = []
        for name, tensor in header.items():
            if name == "__metadata__":
                continue
            start, end = tensor["data_offsets"]
            shape = tensor["shape"]
            if any(type(dim) is not int or dim < 0 for dim in shape):
                return False
            if type(start) is not int or type(end) is not int or start < 0 or end < start:
                return False
            if end - start != math.prod(shape) * sizes[tensor["dtype"]]:
                return False
            offsets.append((start, end))
        if not offsets:
            return False
        end_offset = 0
        for start, end in sorted(offsets):
            if start != end_offset:
                return False
            end_offset = end
        return end_offset > 0 and path.stat().st_size == 8 + length + end_offset
    # Modern torch.save ZIPs have a pickle metadata entry and nonempty tensors.
    # Never unpickle these user-controlled files during a readiness request.
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        return any(item.filename.endswith("/data.pkl") and item.file_size > 0 for item in entries) and any(
            "/data/" in item.filename and item.file_size > 0 for item in entries
        )


def local_model_is_complete(path: Path | str) -> bool:
    target = Path(path)
    try:
        if not target.is_dir():
            return False
        from infrastructure.ai.embedding_download import MANIFEST_NAME, get_model_status
        if (target / MANIFEST_NAME).exists():
            return bool(get_model_status(target)["model_ready"])
        config = _json(target / "config.json")
        if not isinstance(config, dict) or not config.get("model_type"):
            return False
        if (target / "tokenizer.json").is_file():
            tokenizer = _json(target / "tokenizer.json")
            if not isinstance(tokenizer, dict) or not tokenizer.get("model"):
                return False
        elif not (target / "vocab.txt").is_file() or not (target / "vocab.txt").stat().st_size:
            return False
        if (target / "modules.json").exists():
            modules = _json(target / "modules.json")
            if not isinstance(modules, list) or not modules:
                return False
            for module in modules:
                relative = Path(module.get("path", ""))
                if relative.is_absolute() or ".." in relative.parts:
                    return False
                if relative.parts and not isinstance(_json(target / relative / "config.json"), dict):
                    return False
        for name in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
            if (target / name).exists():
                weight_map = _json(target / name)["weight_map"]
                if not isinstance(weight_map, dict) or not weight_map:
                    return False
                shards = set(weight_map.values())
                return all(isinstance(shard, str) and Path(shard).name == shard and
                           _weight_complete(target / shard) for shard in shards)
        return any(_weight_complete(target / name) for name in ("model.safetensors", "pytorch_model.bin")
                   if (target / name).is_file())
    except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error, zipfile.BadZipFile):
        return False
