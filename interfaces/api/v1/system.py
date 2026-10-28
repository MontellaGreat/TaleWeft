"""系统管理 API（内部工具端点）

提供扩展包安装、环境检测等系统级操作。
所有端点仅允许本机访问（localhost）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from interfaces.api.middleware.database_maintenance import DatabaseMaintenanceGate, DatabaseMaintenanceConflict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"], include_in_schema=False)
database_maintenance_gate = DatabaseMaintenanceGate()


@contextmanager
def _maintenance(request: Request):
    """Quiesce all local writers for a consistent application backup."""
    if request.scope.get("database_maintenance_token") is None:
        yield
        return
    with database_maintenance_gate.exclusive(request.scope):
        main = __import__("sys").modules.get("interfaces.main")
        lifecycle = None
        daemon_was_running = False
        if main is not None:
            try:
                manager = main._get_daemon_manager()
                daemon_was_running = manager.status().running
                main._stop_autopilot_daemon_thread()
                lifecycle = main._get_lifecycle()
                lifecycle.stop_background_tasks()
                lifecycle.stop_async_bridge()
                lifecycle.stop_managed_resources()
                lifecycle.close_database(skip_checkpoint=True)
            except Exception:
                # A failed quiesce must not be presented as a verified backup.
                raise
        try:
            yield
        finally:
            if lifecycle is not None:
                lifecycle.bootstrap_persistence_consumer()
                if daemon_was_running:
                    main._start_autopilot_daemon_thread()


def _get_database_backup_service():
    from application.snapshot.services.database_backup_service import DatabaseBackupService
    from application.paths import get_db_path, resolve_runtime_data_path
    from interfaces.api.settings import get_backend_settings
    settings = get_backend_settings().vector_store
    if settings.use_qdrant:
        return DatabaseBackupService(get_db_path(), vector_mode="external")
    vector_path = resolve_runtime_data_path(settings.persist_directory) if settings.enabled else None
    return DatabaseBackupService(get_db_path(), vector_path=vector_path, vector_mode="disabled" if not settings.enabled else "not_configured")


class RestoreRequest(BaseModel):
    expected_sha256: str


@router.get("/database-backups")
def list_database_backups(request: Request):
    _assert_localhost(request)
    return _get_database_backup_service().list()


@router.post("/database-backups")
def create_database_backup(request: Request):
    _assert_localhost(request)
    try:
        with _maintenance(request):
            return _get_database_backup_service().create()
    except DatabaseMaintenanceConflict as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/database-backups/{backup_id}/verify")
def verify_database_backup(backup_id: str, request: Request):
    _assert_localhost(request)
    try:
        return _get_database_backup_service().verify(backup_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/database-backups/{backup_id}/restore-copy")
def restore_database_backup_copy(backup_id: str, request: Request):
    _assert_localhost(request)
    try:
        return _get_database_backup_service().restore_copy(backup_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/database-backups/{backup_id}/restore-request")
def request_database_backup_restore(backup_id: str, body: RestoreRequest, request: Request):
    _assert_localhost(request)
    try:
        return _get_database_backup_service().request_restore(backup_id, expected_sha256=body.expected_sha256)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/database-restore-status")
def database_restore_status(request: Request):
    _assert_localhost(request)
    return _get_database_backup_service().restore_status()


@router.delete("/database-restore-request")
def cancel_database_backup_restore(request: Request):
    _assert_localhost(request)
    try:
        return _get_database_backup_service().cancel_restore()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/aof-recovery-jobs")
def list_aof_recovery_jobs(
    request: Request,
    novel_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Inspect durable append-only chapter recovery state after a restart."""
    _assert_localhost(request)
    from application.engine.services.draft_aof import list_recovery_jobs
    return {"jobs": list_recovery_jobs(novel_id=novel_id, limit=limit)}


@router.post("/aof-recovery-jobs/{job_id}/retry")
def retry_aof_recovery_job(job_id: str, request: Request):
    """Retry a failed/conflicted AOF job without deleting its evidence."""
    _assert_localhost(request)
    from application.engine.services.draft_aof import run_recovery_job
    try:
        job = run_recovery_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="AOF recovery job not found")
    return job

# ════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════

PIP_MIRRORS = [
    {"name": "阿里云",   "url": "https://mirrors.aliyun.com/pypi/simple/",        "host": "mirrors.aliyun.com"},
    {"name": "清华大学", "url": "https://pypi.tuna.tsinghua.edu.cn/simple/",       "host": "pypi.tuna.tsinghua.edu.cn"},
    {"name": "中科大",   "url": "https://pypi.mirrors.ustc.edu.cn/simple/",         "host": "pypi.mirrors.ustc.edu.cn"},
    {"name": "腾讯云",   "url": "https://mirrors.cloud.tencent.com/pypi/simple/",   "host": "mirrors.cloud.tencent.com"},
    {"name": "华为云",   "url": "https://repo.huaweicloud.com/repository/pypi/simple/", "host": "repo.huaweicloud.com"},
    {"name": "官方源",   "url": "https://pypi.org/simple/",                          "host": "pypi.org"},
]

# 全局安装锁：防止并发执行多个 pip install
_install_lock = threading.Lock()
_external_process_lock = threading.Lock()
_INSTALL_HEARTBEAT_SECONDS = 5
_install_status: dict = {
    "running": False,
    "started_at": None,
    "progress_msg": "",
}


def _assert_localhost(request: Request) -> None:
    """仅允许本机访问"""
    if not request.client:
        raise HTTPException(status_code=403, detail="Forbidden")
    host = request.client.host or ""
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        raise HTTPException(status_code=403, detail="Only localhost access allowed")


def _get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).resolve().parents[3]


def _get_python_executable() -> str:
    """Find a real pip-capable Python with the backend's ABI, never its frozen exe."""
    probe = (
        "import importlib.util,json,struct,sys;"
        "print(json.dumps({'abi':sys.implementation.cache_tag,'bits':struct.calcsize('P')*8,"
        "'pip':importlib.util.find_spec('pip') is not None}))"
    )
    import struct
    for candidate in _python_candidates():
        if getattr(sys, "frozen", False) and Path(candidate).resolve() == Path(sys.executable).resolve():
            continue
        try:
            proc = _popen_external_python(
                [candidate, "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            try:
                stdout, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                _stop_install_process(proc)
                raise
            data = json.loads(stdout.strip())
            if proc.returncode == 0 and data.get("pip") and data.get("abi") == sys.implementation.cache_tag and data.get("bits") == struct.calcsize("P") * 8:
                return candidate
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError(
        f"未找到与后端兼容的 Python {sys.version_info.major}.{sys.version_info.minor}（含 pip）。"
        "请安装对应版本 Python，并设置 PLOTPILOT_PYTHON_EXE 指向其 python.exe 后重启应用；"
        "冻结版后端程序不能执行 pip。"
    )


def _python_candidates() -> list[str]:
    root = _get_project_root()
    candidates = [os.getenv("PLOTPILOT_PYTHON_EXE", ""), os.getenv("PYTHON314_EXE", "")]
    if not getattr(sys, "frozen", False):
        candidates.insert(0, sys.executable)
    candidates.extend(str(path) for path in (
        root / ".venv/Scripts/python.exe", root / ".venv/bin/python",
        root / "tools/python_embed/python.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs/Python/Python314/python.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Python/pythoncore-3.14-64/python.exe",
    ))
    candidates.extend(shutil.which(name) or "" for name in ("python", "python3", "python3.14"))
    return list(dict.fromkeys(path.strip('"') for path in candidates if path))


@contextmanager
def _external_python_dll_search():
    """Temporarily undo the Windows bootloader's inherited DLL directory.

    Follow PyInstaller's external-program guidance. Restore immediately after
    CreateProcess, before waiting for the child or processing its output.
    """
    if not (getattr(sys, "frozen", False) and os.name == "nt"):
        yield
        return
    import ctypes
    from ctypes import wintypes
    with _external_process_lock:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
        api.GetDllDirectoryW.restype = wintypes.DWORD
        api.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
        api.SetDllDirectoryW.restype = wintypes.BOOL
        buffer = ctypes.create_unicode_buffer(32768)
        length = api.GetDllDirectoryW(len(buffer), buffer)
        if length >= len(buffer):
            raise OSError("无法安全读取当前 DLL 搜索目录")
        previous = buffer.value if length else None
        if not api.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not api.SetDllDirectoryW(previous):
                raise ctypes.WinError(ctypes.get_last_error())


def _popen_external_python(cmd: list[str], **kwargs):
    """Launch real Python with a sanitized child environment, preserving ours."""
    env = dict(kwargs.pop("env", os.environ))
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    if getattr(sys, "frozen", False):
        bundle_raw = getattr(sys, "_MEIPASS", "")
        if bundle_raw:
            bundle = os.path.normcase(os.path.abspath(bundle_raw))
            def external_path(path):
                try:
                    candidate = os.path.normcase(os.path.abspath(path.strip('"')))
                    return os.path.commonpath((candidate, bundle)) != bundle
                except (OSError, ValueError):
                    return True
            for name in ("PATH", "DYLD_LIBRARY_PATH"):
                if name in env:
                    env[name] = os.pathsep.join(path for path in env[name].split(os.pathsep) if external_path(path))
        if os.name != "nt" and "LD_LIBRARY_PATH" in env:
            original = env.get("LD_LIBRARY_PATH_ORIG")
            if original:
                env["LD_LIBRARY_PATH"] = original
            else:
                env.pop("LD_LIBRARY_PATH", None)
    kwargs["env"] = env
    # timeout is consumed by communicate(), never by the Popen constructor.
    kwargs.pop("timeout", None)
    proc = None
    try:
        with _external_python_dll_search():
            proc = subprocess.Popen(cmd, **kwargs)
        return proc
    except BaseException:
        if proc is not None:
            _stop_install_process(proc)
        raise


def _check_extensions_installed() -> dict:
    """Check packages visible to this backend without importing torch."""
    from infrastructure.ai.local_packages import get_local_packages_status
    return get_local_packages_status()


def _tail_log_file_lines(
    log_path_raw: str,
    max_lines: int,
    max_bytes: int = 768_000,
) -> Tuple[List[str], str, bool, bool]:
    """尾部若干行：(lines, resolved_path, missing, truncated_early_content)"""
    raw = Path(log_path_raw)
    path = raw.resolve() if raw.is_absolute() else (Path.cwd() / raw).resolve()
    resolved = str(path)
    if not path.is_file():
        return [], resolved, True, False
    try:
        size = path.stat().st_size
    except OSError:
        return [], resolved, True, False
    start = max(0, size - max_bytes)
    truncated_window = start > 0
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return [], resolved, True, truncated_window
    if start > 0 and b"\n" in chunk:
        chunk = chunk.split(b"\n", 1)[1]
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    line_truncated = len(lines) > max_lines
    if line_truncated:
        lines = lines[-max_lines:]
    return lines, resolved, False, truncated_window or line_truncated


def _snapshot_feedback_bundle_sync(max_lines: int, ring_limit: int) -> Dict[str, Any]:
    """运行于线程池，避免磁盘 IO 卡住事件循环。"""
    from interfaces.api.settings import get_backend_settings

    log_env = get_backend_settings().log_file
    lines, resolved, missing, log_truncated = _tail_log_file_lines(log_env, max_lines)

    backend_release = "unknown"
    backend_build_id = "unknown"
    uptime_sec: Any = None
    try:
        from interfaces import main as app_main

        backend_release = getattr(app_main, "APP_RELEASE_VERSION", backend_release)
        backend_build_id = getattr(app_main, "BACKEND_BUILD_ID", backend_build_id)
        st = getattr(app_main, "STARTUP_TIME", None)
        if isinstance(st, (int, float)):
            uptime_sec = round(time.time() - st, 2)
    except Exception:
        pass

    ring_out: List[Dict[str, Any]] = []
    if ring_limit > 0:
        try:
            from application.engine.services.autopilot_log_ring import snapshot_global_ring

            for entry in snapshot_global_ring(ring_limit):
                ring_out.append(
                    {
                        "seq": entry.seq,
                        "timestamp": entry.timestamp_iso,
                        "level": entry.level,
                        "logger": entry.logger_name,
                        "novel_id": entry.novel_id,
                        "message": (entry.message or "")[:4000],
                    }
                )
        except Exception as e:
            logger.warning("snapshot_global_ring failed: %s", e)

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "backend_release": backend_release,
        "backend_build_id": backend_build_id,
        "backend_uptime_seconds": uptime_sec,
        "log_file_env": log_env,
        "log_file_resolved": resolved,
        "log_file_missing": missing,
        "log_truncated": log_truncated,
        "log_tail_line_count": len(lines),
        "log_tail_lines": lines,
        "memory_ring_recent": ring_out,
    }


# ════════════════════════════════════════════
# Schema
# ════════════════════════════════════════════


class InstallExtensionsResponse(BaseModel):
    status: str  # "already_running" | "started"
    message: str


# ════════════════════════════════════════════
# 端点
# ════════════════════════════════════════════


@router.get("/feedback-log-snapshot")
async def feedback_log_snapshot(
    request: Request,
    max_lines: int = Query(600, ge=1, le=2500),
    ring_limit: int = Query(200, ge=0, le=800),
):
    """供前端一键反馈：LOG_FILE 尾部 + API 进程内存日志环。

    与 /system/extensions-* 一致，仅限本机（127.0.0.1 / ::1）访问，减轻远程泄漏风险。
    """
    _assert_localhost(request)
    return await asyncio.to_thread(_snapshot_feedback_bundle_sync, max_lines, ring_limit)


def _extensions_snapshot() -> dict:
    from infrastructure.ai.embedding_download import get_model_status
    from infrastructure.ai.local_model_validation import local_model_is_complete, resolve_local_model_path
    from application.ai.embedding_config_service import EmbeddingConfigService
    installed = _check_extensions_installed()
    downloaded = get_model_status()
    try:
        config = EmbeddingConfigService().get_config_readonly()
        selected_path = resolve_local_model_path(config.model_path)
        model = get_model_status(selected_path)
        if not (selected_path / ".plotpilot-model.json").exists() and local_model_is_complete(selected_path):
            model.update(model_ready=True, reason="", download_source=None)
    except Exception:
        # A missing/unavailable settings database must not turn this environment
        # check into a 500 or initialize/migrate a configuration as a side effect.
        model = downloaded
    return {
        **installed, **model,
        "local_ready": installed["all_installed"] and model["model_ready"],
        "downloaded_model_ready": downloaded["model_ready"],
        "downloaded_model_path": downloaded["model_path"],
        "install_running": _install_status["running"],
        "install_progress": _install_status["progress_msg"] if _install_status["running"] else "",
    }


@router.get("/extensions-status")
async def get_extensions_status(request: Request):
    """Check dependencies and verified model files separately."""
    _assert_localhost(request)
    return await asyncio.to_thread(_extensions_snapshot)


def _stop_install_process(proc) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class _DependencyFilesInUseError(RuntimeError):
    pass


def _run_install_process(cmd: list[str], cancel: threading.Event, emit, *, timeout: float = 1200) -> int:
    """Read pip in a separate thread so quiet downloads can be cancelled/timed out."""
    from infrastructure.ai.embedding_download import DownloadCancelled
    proc = _popen_external_python(
        cmd, cwd=str(_get_project_root()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    lines: queue.Queue = queue.Queue()
    def read_output():
        try:
            for line in proc.stdout:
                lines.put(line.rstrip())
        finally:
            lines.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    started = time.monotonic()
    files_in_use = False
    try:
        while True:
            if cancel.is_set():
                raise DownloadCancelled("本地 AI 准备已取消")
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"依赖操作超过 {int(timeout)} 秒，已停止该进程")
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            if line:
                lowered = line.lower()
                files_in_use = files_in_use or any(marker in lowered for marker in (
                    "winerror 32", "winerror 5", "permissionerror", "permission denied",
                    "being used by another process", "被另一个进程使用",
                ))
                # An unsuccessful mirror is recoverable; only the final done
                # event determines failure for the complete preparation task.
                emit({"type": "log", "phase": "dependencies", "message": line[:600]})
        code = proc.wait(timeout=10)
        if code != 0 and files_in_use:
            raise _DependencyFilesInUseError("本地 AI 依赖文件被占用或没有写入权限。请完全退出并重启应用后重试；仍失败时请检查数据目录权限。")
        return code
    finally:
        _stop_install_process(proc)
        reader.join(timeout=2)
        if proc.stdout:
            proc.stdout.close()


def _prepare_local_engine(emit, cancel: threading.Event) -> dict:
    from infrastructure.ai.embedding_download import EmbeddingModelDownloader, DownloadCancelled
    from infrastructure.ai.local_packages import ensure_local_packages_path
    import importlib

    def wait_for_dependencies():
        deadline = time.monotonic() + 180
        while True:
            status = _check_extensions_installed()
            if status.get("check_timed_out"):
                raise RuntimeError(status["dependency_error"])
            if not status.get("checking"):
                return status
            if cancel.wait(0.2):
                raise DownloadCancelled("本地 AI 准备已取消")
            if time.monotonic() > deadline:
                raise RuntimeError("本地 AI 依赖验证超时，请重启应用后重试")

    emit({"type": "info", "phase": "dependencies", "message": "检查本地 AI 运行环境…"})
    installed = wait_for_dependencies()
    if not installed["all_installed"]:
        root = _get_project_root()
        req_file = root / "requirements-local.txt"
        if not req_file.is_file() and not getattr(sys, "frozen", False):
            raise RuntimeError(f"找不到 {req_file.name}，请恢复项目扩展依赖清单后重试")
        python = _get_python_executable()
        target = ensure_local_packages_path()
        target.mkdir(parents=True, exist_ok=True)
        marker = target / ".plotpilot-packages.json"
        marker.unlink(missing_ok=True)
        ensure_local_packages_path()
        emit({"type": "info", "phase": "dependencies", "message": "开始准备本地 AI 依赖（首次下载可能需要 5–20 分钟）"})
        # Frozen resources may omit requirements-local.txt. Keep the same small
        # dependency list in the command; downloads always go to writable data.
        packages = ["-r", str(req_file)] if req_file.is_file() else ["numpy>=1.21.0", "faiss-cpu>=1.7.4", "sentence-transformers>=2.2.0"]
        success = False
        for mirror in PIP_MIRRORS:
            if cancel.is_set():
                raise DownloadCancelled("本地 AI 准备已取消")
            emit({"type": "info", "phase": "dependencies", "message": f"准备依赖 [{mirror['name']}]…"})
            cmd = [python, "-m", "pip", "install", *packages,
                   "--target", str(target), "--upgrade", "--no-warn-script-location",
                   "-i", mirror["url"], "--timeout", "30", "--retries", "1",
                   "--no-cache-dir", "--progress-bar", "off", "--disable-pip-version-check"]
            try:
                if _run_install_process(cmd, cancel, emit) == 0:
                    success = True
                    break
            except DownloadCancelled:
                raise
            except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
                emit({"type": "warn", "phase": "dependencies", "message": f"依赖源 {mirror['name']} 失败: {exc}"})
            else:
                emit({"type": "warn", "phase": "dependencies", "message": f"依赖源 {mirror['name']} 安装失败，自动切换来源"})
        if not success:
            raise RuntimeError("所有依赖下载源均失败，请检查网络、磁盘空间和 Python 版本后重试")
        # Verify the installed native wheels using a real, ABI-compatible Python.
        # Never run the frozen backend executable with -c/-m arguments.
        verify_code = (
            "import sys;sys.path.insert(0," + repr(str(target)) + ");"
            "import numpy,faiss,torch,transformers,tokenizers,safetensors,sentence_transformers"
        )
        emit({"type": "info", "phase": "dependencies", "message": "验证本地 AI 依赖能否加载…"})
        if _run_install_process([python, "-c", verify_code], cancel, emit, timeout=180) != 0:
            raise RuntimeError("本地 AI 依赖加载失败，请检查上方日志中的 Python 或动态库错误后重试")
        marker.write_text(json.dumps({"abi": sys.implementation.cache_tag, "verified": True}), encoding="utf-8")
        ensure_local_packages_path()
        importlib.invalidate_caches()
        installed = wait_for_dependencies()
        if not installed["all_installed"]:
            raise RuntimeError("安装后的依赖未能被当前后端识别，请重启应用后重试")
    else:
        emit({"type": "info", "phase": "dependencies", "message": "本地 AI 依赖已就绪，检查模型文件…"})
    model = EmbeddingModelDownloader().download(emit, cancel)
    return {**installed, **model, "local_ready": installed["all_installed"] and model["model_ready"]}


class _InstallStreamingResponse(StreamingResponse):
    """Own worker cancellation even if the network consumer stops at a yield.

    Starlette can exit its send loop with the async generator still suspended:
    ASGI 2.4 raises on send; older ASGI cancels the send consumer on disconnect.
    Closing at this outer boundary avoids relying on garbage collection.
    """
    def __init__(self, content, *, cancel_event: threading.Event, **kwargs):
        super().__init__(content, **kwargs)
        self._cancel_event = cancel_event

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._cancel_event.set()
            await self.body_iterator.aclose()


@router.post("/install-extensions")
async def install_extensions(request: Request):
    """Prepare dependencies and a verified local model, with cancellable SSE."""
    _assert_localhost(request)
    if not _install_lock.acquire(blocking=False):
        # Keep POST consistently SSE: JSON here previously looked like a broken
        # stream to callers that were already expecting an installation result.
        async def already_running():
            yield "data: " + json.dumps({"type": "done", "success": False, "message": "安装任务已在运行中，请等待完成"}, ensure_ascii=False) + "\n\n"
        return StreamingResponse(already_running(), media_type="text/event-stream")

    events: queue.Queue = queue.Queue()
    cancel = threading.Event()
    _install_status.update(running=True, started_at=time.time(), progress_msg="准备本地 AI…")

    def emit(event):
        if event.get("message"):
            _install_status["progress_msg"] = event["message"]
        events.put(event)

    def worker():
        try:
            result = _prepare_local_engine(emit, cancel)
            terminal = {"type": "done", "success": True, "message": "本地 AI 依赖和模型已准备完成", "installed": result, **result}
        except Exception as exc:
            logger.warning("Local AI preparation failed: %s", exc)
            emit({"type": "error", "message": str(exc)})
            terminal = {"type": "done", "success": False, "message": str(exc)}
        finally:
            _install_status.update(running=False, progress_msg="")
            _install_lock.release()
        events.put(terminal)

    threading.Thread(target=worker, name="local-ai-preparation", daemon=True).start()

    async def generate_events():
        last_event = time.monotonic()
        try:
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    if time.monotonic() - last_event >= _INSTALL_HEARTBEAT_SECONDS:
                        yield ": keep-alive\n\n"
                        last_event = time.monotonic()
                    await asyncio.sleep(0.1)
                    continue
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                last_event = time.monotonic()
                if event.get("type") == "done":
                    break
        finally:
            # StreamingResponse closes this iterator on client disconnect.
            # Both pip and the HTTP downloader observe this cancellation token.
            cancel.set()

    return _InstallStreamingResponse(
        generate_events(), cancel_event=cancel, media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
