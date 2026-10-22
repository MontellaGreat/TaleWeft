"""Public sample imports preserve author edits and never need a model or network."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "install_sample_book.py"
SAMPLE_ID = "taleweft-sample-fog-harbor"
RECEIPT = "sample-book-receipt.json"


def run_installer(*args):
    # Blocking real sockets proves this fixture can be installed without an API.
    code = """import runpy, socket, sys, httpx
def deny_network(*args, **kwargs):
    raise AssertionError('Sample installation must not access the network')
original_connect = socket.socket.connect
def local_socket_pair_only(sock, address):
    if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return original_connect(sock, address)
    return deny_network()
socket.socket.connect = local_socket_pair_only
socket.create_connection = deny_network
httpx.HTTPTransport.handle_request = deny_network
httpx.AsyncHTTPTransport.handle_async_request = deny_network
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-c", code, str(SCRIPT), *map(str, args)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=90)


@pytest.fixture(scope="module")
def installed_sample(tmp_path_factory):
    target = tmp_path_factory.mktemp("public-sample")
    result = run_installer("--data-dir", target)
    assert result.returncode == 0, result.stdout + result.stderr
    return target


def test_cli_requires_explicit_data_directory():
    result = run_installer()
    assert result.returncode == 2
    assert "--data-dir" in result.stderr


def test_offline_install_contains_complete_public_sample_and_no_model_profiles(installed_sample):
    with sqlite3.connect(installed_sample / "plotpilot.db") as db:
        assert db.execute("SELECT title FROM novels WHERE id=?", (SAMPLE_ID,)).fetchone() == ("雾港来信·功能测试",)
        chapters = db.execute("SELECT number,title,content FROM chapters WHERE novel_id=? ORDER BY number", (SAMPLE_ID,)).fetchall()
        assert [chapter[0] for chapter in chapters] == list(range(1, 7))
        assert [bool(chapter[2].strip()) for chapter in chapters] == [True, True, False, False, False, False]
        for table in ("bibles", "bible_locations", "bible_world_settings", "unified_characters", "unified_props", "story_nodes", "novel_snapshots", "chapter_reviews", "chapter_entity_mentions"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE novel_id=?", (SAMPLE_ID,)).fetchone()[0] > 0
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "llm_profiles" not in tables or db.execute("SELECT COUNT(*) FROM llm_profiles").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    credentials = installed_sample / ".credentials"
    assert not credentials.exists() or not list(credentials.iterdir())
    receipt = json.loads((installed_sample / RECEIPT).read_text(encoding="utf-8"))
    assert receipt["novel_id"] == SAMPLE_ID and receipt["completed_chapters"] == 2
    assert receipt["model_invocations"] == 0


def test_repeated_install_preserves_manual_edits_and_creates_no_duplicate(installed_sample, tmp_path):
    target = tmp_path / "edited"
    shutil.copytree(installed_sample, target)
    with sqlite3.connect(target / "plotpilot.db") as db:
        db.execute("UPDATE chapters SET title=?,content=? WHERE novel_id=? AND number=1", ("作者改名", "这是作者手动修改的正文。", SAMPLE_ID))
    result = run_installer("--data-dir", target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "existing edits preserved" in result.stdout
    with sqlite3.connect(target / "plotpilot.db") as db:
        assert db.execute("SELECT title,content FROM chapters WHERE novel_id=? AND number=1", (SAMPLE_ID,)).fetchone() == ("作者改名", "这是作者手动修改的正文。")
        assert db.execute("SELECT COUNT(*) FROM novels").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chapters").fetchone() == (6,)


def test_existing_sample_without_completed_receipt_is_never_overwritten(installed_sample, tmp_path):
    target = tmp_path / "partial"
    shutil.copytree(installed_sample, target)
    (target / RECEIPT).unlink()
    result = run_installer("--data-dir", target)
    assert result.returncode != 0
    assert "preserved" in result.stderr
    with sqlite3.connect(target / "plotpilot.db") as db:
        assert db.execute("SELECT COUNT(*) FROM novels").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chapters").fetchone() == (6,)


def test_same_title_with_another_identity_blocks_import_and_preserves_profile(installed_sample, tmp_path):
    target = tmp_path / "existing-library"
    shutil.copytree(installed_sample, target)
    (target / RECEIPT).unlink()
    with sqlite3.connect(target / "plotpilot.db") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM novels WHERE id=?", (SAMPLE_ID,))
        db.execute("INSERT INTO novels(id,slug,title,autopilot_status) VALUES (?,?,?,'running')", ("author-book", "author-book", "雾港来信·功能测试"))
        columns = [row[1] for row in db.execute("PRAGMA table_info(llm_profiles)")]
        # Use the actual schema defaults, with deliberately non-secret sample values.
        values = {"id": "author-profile", "name": "Existing configuration", "provider": "openai", "model": "existing-model", "base_url": "https://example.invalid/v1"}
        fields = [field for field in values if field in columns]
        db.execute(f"INSERT INTO llm_profiles({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", [values[field] for field in fields])
        before = db.execute("SELECT * FROM llm_profiles").fetchall()
    result = run_installer("--data-dir", target)
    assert result.returncode != 0
    assert "sample title already exists" in result.stderr
    with sqlite3.connect(target / "plotpilot.db") as db:
        assert db.execute("SELECT id,autopilot_status FROM novels").fetchall() == [("author-book", "running")]
        assert db.execute("SELECT * FROM llm_profiles").fetchall() == before


def test_successful_import_preserves_other_books_and_model_profiles(installed_sample, tmp_path):
    target = tmp_path / "existing-author-library"
    shutil.copytree(installed_sample, target)
    (target / RECEIPT).unlink()
    with sqlite3.connect(target / "plotpilot.db") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM novels WHERE id=?", (SAMPLE_ID,))
        db.execute("INSERT INTO novels(id,slug,title,autopilot_status) VALUES (?,?,?,'running')", ("author-book", "author-book", "作者原有作品"))
        db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES (?,?,1,?,?)", ("author-chapter", "author-book", "原有章节", "保留作者正文。"))
        db.execute("INSERT INTO llm_profiles(id,name,model,base_url) VALUES (?,?,?,?)", ("author-profile", "Existing configuration", "existing-model", "https://example.invalid/v1"))
        books_before = db.execute("SELECT * FROM novels WHERE id='author-book'").fetchall()
        chapters_before = db.execute("SELECT * FROM chapters WHERE novel_id='author-book'").fetchall()
        profiles_before = db.execute("SELECT * FROM llm_profiles").fetchall()
    result = run_installer("--data-dir", target)
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(target / "plotpilot.db") as db:
        assert db.execute("SELECT COUNT(*) FROM novels").fetchone() == (2,)
        assert db.execute("SELECT * FROM novels WHERE id='author-book'").fetchall() == books_before
        assert db.execute("SELECT * FROM chapters WHERE novel_id='author-book'").fetchall() == chapters_before
        assert db.execute("SELECT * FROM llm_profiles").fetchall() == profiles_before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_running_instance_ownership_blocks_offline_install(tmp_path):
    from infrastructure.persistence.database.recovery_ownership import DatabaseOwnership
    with DatabaseOwnership(tmp_path / "plotpilot.db"):
        result = run_installer("--data-dir", tmp_path)
    assert result.returncode != 0
    assert "close" in result.stderr.lower() or "活跃实例" in result.stderr
    assert not (tmp_path / "plotpilot.db").exists()


def test_public_manuscript_is_valid_and_contains_only_metadata_and_chapters():
    from application.core.services.manuscript_transfer import validate_document
    path = ROOT / "examples" / "fog-harbor.taleweft.json"
    assert path.is_file(), "The portable public manuscript is missing"
    document, report = validate_document(path.read_text(encoding="utf-8"))
    assert report["chapter_count"] == 6 and report["empty_chapter_count"] == 4
    assert set(document) == {"format", "format_version", "novel", "chapters", "payload_sha256"}
