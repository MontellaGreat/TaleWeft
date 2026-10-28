"""Installation SSE must terminate explicitly and release its installation lock."""
import asyncio
import json
import threading
import pytest

from starlette.requests import Request

from interfaces.api.v1 import system


def local_request():
    return Request({"type": "http", "client": ("127.0.0.1", 1234), "headers": [], "method": "POST", "path": "/"})


def collect_events(response):
    async def collect():
        result = []
        async for chunk in response.body_iterator:
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    result.append(json.loads(line[6:]))
        return result
    return asyncio.run(collect())


def test_install_failure_sends_done_and_releases_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(system, "_check_extensions_installed", lambda: {"all_installed": False})
    monkeypatch.setattr(system, "_install_lock", threading.Lock())
    response = asyncio.run(system.install_extensions(local_request()))
    events = collect_events(response)
    assert events[-1]["type"] == "done"
    assert events[-1]["success"] is False
    assert not system._install_lock.locked()
    assert system._install_status["running"] is False


def test_frozen_backend_is_never_used_as_python(monkeypatch, tmp_path):
    monkeypatch.setattr(system.sys, "frozen", True, raising=False)
    monkeypatch.setattr(system.sys, "executable", str(tmp_path / "plotpilot-backend.exe"))
    monkeypatch.delenv("PLOTPILOT_PYTHON_EXE", raising=False)
    monkeypatch.setattr(system, "_python_candidates", lambda: [])
    import pytest
    with pytest.raises(RuntimeError, match="PLOTPILOT_PYTHON_EXE"):
        system._get_python_executable()


def test_concurrent_install_returns_terminal_sse(monkeypatch):
    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(system, "_install_lock", lock)
    try:
        response = asyncio.run(system.install_extensions(local_request()))
        events = collect_events(response)
        assert events[-1]["type"] == "done"
        assert events[-1]["success"] is False
    finally:
        lock.release()


def test_closing_sse_cancels_worker_and_releases_lock(monkeypatch):
    cancelled = threading.Event()
    lock = threading.Lock()
    monkeypatch.setattr(system, "_install_lock", lock)
    def prepare(emit, cancel):
        emit({"type": "info", "message": "waiting"})
        assert cancel.wait(5)
        cancelled.set()
        raise RuntimeError("cancelled")
    monkeypatch.setattr(system, "_prepare_local_engine", prepare)
    async def run():
        response = await system.install_extensions(local_request())
        iterator = response.body_iterator
        await anext(iterator)
        await iterator.aclose()
    asyncio.run(run())
    assert cancelled.wait(2)
    assert lock.acquire(timeout=2)
    lock.release()


def test_status_reports_selected_custom_model_instead_of_default_download(monkeypatch, tmp_path):
    from application.ai.embedding_config_service import EmbeddingConfigService, EmbeddingConfigModel
    import struct
    custom = tmp_path / "custom-model"
    custom.mkdir()
    (custom / "config.json").write_text('{"model_type":"bert"}', encoding="utf-8")
    (custom / "tokenizer.json").write_text('{"model":{"type":"WordPiece"}}', encoding="utf-8")
    header = b'{"tensor":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}'
    (custom / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\0\0\0\0")
    monkeypatch.setattr(EmbeddingConfigService, "get_config_readonly", lambda self: EmbeddingConfigModel(model_path=str(custom)))
    monkeypatch.setattr(system, "_check_extensions_installed", lambda: {"all_installed": True})
    status = system._extensions_snapshot()
    assert status["model_path"] == str(custom)
    assert status["model_ready"] is True
    assert status["local_ready"] is True


def test_quiet_install_emits_heartbeat_and_still_cancels(monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(system, "_install_lock", lock)
    monkeypatch.setattr(system, "_INSTALL_HEARTBEAT_SECONDS", 0.01, raising=False)
    def prepare(emit, cancel):
        assert cancel.wait(5)
        raise RuntimeError("cancelled")
    monkeypatch.setattr(system, "_prepare_local_engine", prepare)
    async def run():
        response = await system.install_extensions(local_request())
        iterator = response.body_iterator
        chunk = await asyncio.wait_for(anext(iterator), timeout=0.5)
        assert chunk == ": keep-alive\n\n"
        await iterator.aclose()
    asyncio.run(run())
    assert lock.acquire(timeout=2)
    lock.release()


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_asgi_socket_disconnect_cancels_install_even_with_retained_response(monkeypatch, spec_version):
    """A send failure/cancelled consumer does not itself close an async generator."""
    from starlette.requests import ClientDisconnect
    lock = threading.Lock()
    captured = {}
    monkeypatch.setattr(system, "_install_lock", lock)
    def prepare(emit, cancel):
        captured["cancel"] = cancel
        emit({"type": "info", "message": "waiting for network consumer"})
        cancel.wait(5)
        raise RuntimeError("diagnostic cancellation")
    monkeypatch.setattr(system, "_prepare_local_engine", prepare)
    async def run():
        scope = {"type": "http", "asgi": {"spec_version": spec_version}, "client": ("127.0.0.1", 1234), "headers": [], "method": "POST", "path": "/"}
        response = await system.install_extensions(Request(scope))
        sent = asyncio.Event()
        async def send(message):
            if message["type"] == "http.response.body":
                sent.set()
                if spec_version == "2.4":
                    raise OSError("disconnected socket")
                # Leave the generator suspended at yield while the ASGI 2.3
                # disconnect listener cancels its network consumer.
                await asyncio.sleep(10)
        async def receive():
            await sent.wait()
            return {"type": "http.disconnect"}
        try:
            if spec_version == "2.4":
                with pytest.raises(ClientDisconnect):
                    await response(scope, receive, send)
            else:
                await response(scope, receive, send)
            assert captured["cancel"].is_set()
        finally:
            # Keep the response alive through the assertion: GC cleanup must not
            # be what cancels a multi-gigabyte download. Clean up on RED too.
            await response.body_iterator.aclose()
    asyncio.run(run())
    assert lock.acquire(timeout=2)
    lock.release()


def dll_environment(monkeypatch, tmp_path):
    """Model the inherited Windows DLL directory at the native API boundary."""
    import ctypes
    from types import SimpleNamespace
    bundle = tmp_path / "_internal"
    previous = str(bundle)
    state = {"path": previous}
    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *args):
            return self.call(*args)
    def get_directory(capacity, buffer):
        buffer.value = state["path"] or ""
        return len(buffer.value)
    def set_directory(path):
        state["path"] = path
        return 1
    api = SimpleNamespace(GetDllDirectoryW=Function(get_directory), SetDllDirectoryW=Function(set_directory))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api)
    monkeypatch.setattr(system.sys, "frozen", True, raising=False)
    monkeypatch.setattr(system.sys, "_MEIPASS", str(bundle), raising=False)
    clean = str(tmp_path / "external-bin")
    sibling = str(tmp_path / "_internal-other")
    inherited_path = system.os.pathsep.join((previous, str(bundle / "vendor"), clean, sibling))
    monkeypatch.setenv("PATH", inherited_path)
    return state, previous, inherited_path, system.os.pathsep.join((clean, sibling))


def test_frozen_probe_and_pip_spawn_with_clean_dll_search_then_restore(monkeypatch, tmp_path):
    import io
    import struct
    state, previous, inherited_path, clean_path = dll_environment(monkeypatch, tmp_path)
    launches = []
    class Process:
        returncode = 0
        stdout = None
        def communicate(self, timeout):
            assert state["path"] == previous, "restore immediately after spawn, before waiting"
            return json.dumps({"abi": system.sys.implementation.cache_tag, "bits": struct.calcsize("P") * 8, "pip": True}), ""
        def poll(self):
            return 0
        def wait(self, timeout):
            return 0
    def spawn(command, **kwargs):
        assert state["path"] is None
        assert kwargs["env"]["PATH"] == clean_path
        launches.append(command)
        proc = Process()
        proc.stdout = io.StringIO("installation output\n")
        return proc
    monkeypatch.setattr(system.subprocess, "Popen", spawn)
    monkeypatch.setattr(system, "_python_candidates", lambda: ["external-python.exe"])
    assert system._get_python_executable() == "external-python.exe"
    assert state["path"] == previous
    assert system._run_install_process(["external-python.exe", "-m", "pip", "--version"], threading.Event(), lambda event: None) == 0
    assert state["path"] == previous
    assert system.os.environ["PATH"] == inherited_path
    assert len(launches) == 2


def test_frozen_dll_directory_is_restored_when_process_creation_fails(monkeypatch, tmp_path):
    state, previous, inherited_path, _ = dll_environment(monkeypatch, tmp_path)
    def fail_spawn(*args, **kwargs):
        assert state["path"] is None
        raise OSError("process creation denied")
    monkeypatch.setattr(system.subprocess, "Popen", fail_spawn)
    with pytest.raises(OSError, match="process creation denied"):
        system._popen_external_python(["external-python.exe"])
    assert state["path"] == previous
    assert system.os.environ["PATH"] == inherited_path


def test_locked_dependency_failure_requests_restart_without_retrying_mirrors(monkeypatch, tmp_path):
    import io
    from infrastructure.ai import local_packages
    (tmp_path / "requirements-local.txt").write_text("numpy\n", encoding="utf-8")
    monkeypatch.setattr(system, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(system, "_get_python_executable", lambda: "external-python.exe")
    monkeypatch.setattr(system, "_check_extensions_installed", lambda: {"all_installed": False})
    monkeypatch.setattr(local_packages, "ensure_local_packages_path", lambda: tmp_path / "packages")
    monkeypatch.setattr(system.sys, "frozen", False, raising=False)
    launches = []
    class Process:
        returncode = 1
        def __init__(self):
            self.stdout = io.StringIO("ERROR: Could not install packages: [WinError 32] The process cannot access the file because it is being used by another process\n")
        def poll(self):
            return 1
        def wait(self, timeout):
            return 1
    def spawn(command, **kwargs):
        launches.append(command)
        return Process()
    monkeypatch.setattr(system.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="重启"):
        system._prepare_local_engine(lambda event: None, threading.Event())
    assert len(launches) == 1
