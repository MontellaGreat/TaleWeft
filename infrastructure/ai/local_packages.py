"""Make optional packages installed in the writable data directory importable."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

OPTIONAL_PACKAGES = ("faiss", "numpy", "sentence_transformers", "torch", "transformers", "tokenizers", "safetensors")
_checks: dict[tuple, dict] = {}
_checks_lock = threading.Lock()
_check_started: dict[tuple, float] = {}
_check_tokens: dict[tuple, object] = {}
_DEPENDENCY_CHECK_TIMEOUT_SECONDS = 180


def ensure_local_packages_path() -> Path:
    from application.paths import resolve_runtime_data_path
    target = resolve_runtime_data_path("local-python-packages")
    try:
        marker = json.loads((target / ".plotpilot-packages.json").read_text(encoding="utf-8"))
        verified = marker.get("verified") is True and marker.get("abi") == sys.implementation.cache_tag
    except (OSError, ValueError, AttributeError):
        verified = False
    if verified and str(target) not in sys.path:
        sys.path.insert(0, str(target))
        importlib.invalidate_caches()
    elif not verified and str(target) in sys.path:
        sys.path.remove(str(target))
        importlib.invalidate_caches()
    return target


def get_local_packages_status() -> dict:
    """Start one cached import check; API callers never wait for torch imports."""
    target = ensure_local_packages_path()
    result = {}
    signature = [str(target)]
    try:
        signature.append((target / ".plotpilot-packages.json").stat().st_mtime_ns)
    except OSError:
        signature.append(None)
    for package in OPTIONAL_PACKAGES:
        try:
            spec = importlib.util.find_spec(package)
            result[package] = spec is not None
            origin = spec.origin if spec else None
            try:
                stat = Path(origin).stat() if origin else None
                signature.append((package, origin, stat.st_size if stat else None, stat.st_mtime_ns if stat else None))
            except OSError:
                signature.append((package, origin))
        except (ImportError, ValueError, AttributeError):
            result[package] = False
            signature.append((package, None))
    if not all(result.values()):
        return {**result, "all_installed": False, "checking": False, "dependency_error": ""}
    key = tuple(signature)
    with _checks_lock:
        if key in _checks:
            if _checks[key]["checking"] and time.monotonic() - _check_started[key] >= _DEPENDENCY_CHECK_TIMEOUT_SECONDS:
                _checks[key] = {**result, "all_installed": False, "checking": False,
                                "check_timed_out": True,
                                "dependency_error": "本地 AI 依赖检查超时，请重启应用后重试"}
            return dict(_checks[key])
        _checks[key] = {**result, "all_installed": False, "checking": True, "dependency_error": ""}
        _check_started[key] = time.monotonic()
        token = object()
        _check_tokens[key] = token
        initial = dict(_checks[key])

    def probe():
        verified = dict(result)
        error = ""
        package = "runtime"
        try:
            for package in tuple(result):
                importlib.import_module(package)
        except BaseException as exc:
            # Native package initializers sometimes raise SystemExit. It must
            # still terminate the check rather than strand checking=True.
            verified[package] = False
            error = f"{package}: {type(exc).__name__}: {exc}"
        finally:
            with _checks_lock:
                if _check_tokens.get(key) is token:
                    _checks[key] = {**verified, "all_installed": not error and all(verified.values()),
                                    "checking": False, "dependency_error": error}
    try:
        threading.Thread(target=probe, name="local-ai-dependency-check", daemon=True).start()
    except Exception as exc:
        with _checks_lock:
            _checks[key] = {**result, "all_installed": False, "checking": False,
                            "dependency_error": f"无法启动依赖检查: {exc}"}
        return dict(_checks[key])
    return initial
