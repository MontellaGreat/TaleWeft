import json
import sys
import threading
import time

from infrastructure.ai import local_packages


def test_incomplete_install_is_not_added_to_backend_import_path(monkeypatch, tmp_path):
    from application import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = tmp_path / "local-python-packages"
    target.mkdir()
    (target / "sentence_transformers.py").write_text("raise RuntimeError('incomplete')", encoding="utf-8")
    local_packages.ensure_local_packages_path()
    assert str(target) not in sys.path


def test_verified_compatible_packages_are_visible_without_importing_them(monkeypatch, tmp_path):
    from application import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = tmp_path / "local-python-packages"
    target.mkdir()
    (target / ".plotpilot-packages.json").write_text(json.dumps({"abi": sys.implementation.cache_tag, "verified": True}), encoding="utf-8")
    (target / "plotpilot_optional_probe.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
    local_packages.ensure_local_packages_path()
    import importlib.util
    assert importlib.util.find_spec("plotpilot_optional_probe") is not None
    assert "plotpilot_optional_probe" not in sys.modules


def test_wrong_abi_install_is_not_added_to_backend_import_path(monkeypatch, tmp_path):
    from application import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = tmp_path / "local-python-packages"
    target.mkdir()
    (target / ".plotpilot-packages.json").write_text(json.dumps({"abi": "cpython-300", "verified": True}), encoding="utf-8")
    local_packages.ensure_local_packages_path()
    assert str(target) not in sys.path


def test_dependency_import_check_returns_immediately_then_reports_broken_package(monkeypatch, tmp_path):
    import builtins
    release = threading.Event()
    monkeypatch.setattr(builtins, "_pp_probe_release", release, raising=False)
    monkeypatch.setattr(local_packages, "OPTIONAL_PACKAGES", ("pp_optional_broken",), raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "pp_optional_broken.py").write_text("import builtins\nbuiltins._pp_probe_release.wait(5)\nraise RuntimeError('native dependency broken')\n", encoding="utf-8")
    started = time.monotonic()
    status = local_packages.get_local_packages_status()
    assert time.monotonic() - started < 1
    assert status["checking"] is True
    assert status["all_installed"] is False
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = local_packages.get_local_packages_status()
        if not status["checking"]:
            break
        time.sleep(0.01)
    assert status["checking"] is False
    assert status["all_installed"] is False
    assert "native dependency broken" in status["dependency_error"]


def test_system_exit_from_optional_import_becomes_terminal_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(local_packages, "OPTIONAL_PACKAGES", ("pp_optional_system_exit",))
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "pp_optional_system_exit.py").write_text("raise SystemExit('native initialization stopped')\n", encoding="utf-8")
    status = local_packages.get_local_packages_status()
    deadline = time.monotonic() + 1
    while status["checking"] and time.monotonic() < deadline:
        time.sleep(0.01)
        status = local_packages.get_local_packages_status()
    assert status["checking"] is False
    assert status["all_installed"] is False
    assert "SystemExit" in status["dependency_error"]


def test_hung_import_has_bounded_failure_and_late_result_does_not_replace_new_key(monkeypatch, tmp_path):
    import builtins
    release = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(builtins, "_pp_slow_release", release, raising=False)
    monkeypatch.setattr(builtins, "_pp_slow_entered", entered, raising=False)
    monkeypatch.setattr(local_packages, "OPTIONAL_PACKAGES", ("pp_optional_deadline",))
    monkeypatch.setattr(local_packages, "_DEPENDENCY_CHECK_TIMEOUT_SECONDS", 0.02, raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "pp_optional_deadline.py").write_text("import builtins\nbuiltins._pp_slow_entered.set()\nbuiltins._pp_slow_release.wait(5)\n", encoding="utf-8")
    (tmp_path / "pp_optional_new_failure.py").write_text("raise RuntimeError('different runtime remains broken')\n", encoding="utf-8")
    try:
        initial = local_packages.get_local_packages_status()
        assert initial["checking"] is True
        assert entered.wait(1)
        time.sleep(0.05)
        timed_out = local_packages.get_local_packages_status()
        assert timed_out["checking"] is False
        assert timed_out["all_installed"] is False
        assert "超时" in timed_out["dependency_error"]
        monkeypatch.setattr(local_packages, "OPTIONAL_PACKAGES", ("pp_optional_new_failure",))
        newer = local_packages.get_local_packages_status()
        deadline = time.monotonic() + 1
        while newer["checking"] and time.monotonic() < deadline:
            time.sleep(0.01)
            newer = local_packages.get_local_packages_status()
        assert "different runtime remains broken" in newer["dependency_error"]
        release.set()
        time.sleep(0.05)
        still_newer = local_packages.get_local_packages_status()
        assert still_newer["all_installed"] is False
        assert "different runtime remains broken" in still_newer["dependency_error"]
    finally:
        release.set()
