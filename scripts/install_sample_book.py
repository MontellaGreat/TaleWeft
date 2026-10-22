"""Install the self-authored sample through existing HTTP routes, without AI.

Run as a separate, offline process; --data-dir is mandatory. No desktop server,
daemon, environment file, model profile or credential is loaded by this tool.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RECEIPT = "sample-book-receipt.json"


def install(data_dir: Path) -> bool:
    """Return False for an already installed sample; preserve all existing edits."""
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    # Must precede any application imports: data identity is cached at import.
    os.environ.update(TALEWEFT_PROD_DATA_DIR=str(data_dir),
                      PLOTPILOT_PROD_DATA_DIR=str(data_dir),
                      PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES="1")

    from application.paths import get_db_path
    from infrastructure.persistence.database.recovery_ownership import DatabaseOwnership

    with DatabaseOwnership(get_db_path()):
        return _install_via_routes(data_dir)


def _install_via_routes(data_dir: Path) -> bool:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from infrastructure.persistence.database.connection import get_database
    from scripts.sample_book_content import CHAPTERS, NOVEL_ID, identity, public_payloads
    from interfaces.api.v1.core import chapters, manuscript_entity_routes, novels
    from interfaces.api.v1.world import bible, knowledge
    from interfaces.api.v1.analyst import foreshadow_ledger
    from interfaces.api.v1.blueprint import story_structure
    from interfaces.api.v1.engine import snapshot_routes

    # Mount the same public routers without the desktop lifespan, which would
    # stop running books and start AI/recovery daemons in an author's library.
    app = FastAPI()
    for router in (novels.router, bible.router, knowledge.router, foreshadow_ledger.router,
                   story_structure.router, snapshot_routes.router):
        app.include_router(router, prefix="/api/v1")
    for router in (chapters.router, manuscript_entity_routes.router):
        app.include_router(router, prefix="/api/v1/novels")
    db = get_database()
    try:
        with TestClient(app) as client:
            def api(method, path, payload=None):
                response = client.request(method, "/api/v1" + path, json=payload)
                if response.status_code >= 400:
                    raise ValueError(f"{method} {path}: HTTP {response.status_code}: {response.text[:500]}")
                return response.json()

            novel, bible_data, knowledge_data = public_payloads()
            existing = api("GET", "/novels/")
            same_id = next((book for book in existing if book["id"] == NOVEL_ID), None)
            receipt_path = data_dir / RECEIPT
            if same_id is not None:
                try:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    receipt = {}
                if receipt.get("novel_id") == NOVEL_ID and receipt.get("status") == "installed":
                    print("Sample already installed; existing edits preserved.")
                    return False
                raise ValueError("Existing sample is preserved; its completed receipt is missing. Inspect it before importing again.")
            if any(book["title"] == novel["title"] for book in existing):
                raise ValueError("A book with the sample title already exists; existing work preserved. Rename that book or choose another data directory.")

            # No AI generation routes are called. Errors leave the partial sample
            # visible and never trigger a destructive retry or deletion.
            api("POST", "/novels/", novel)
            b = f"/bible/novels/{NOVEL_ID}/bible"
            api("POST", b, {"bible_id": identity("bible"), "novel_id": NOVEL_ID})
            api("PUT", b, bible_data)
            for number, (title, content) in enumerate(CHAPTERS, 1):
                if content:
                    api("POST", f"/novels/{NOVEL_ID}/chapters", {
                        "chapter_id": identity(f"chapter-{number}"), "number": number,
                        "title": title, "content": content})
                else:
                    api("POST", f"/novels/{NOVEL_ID}/chapters/{number}/ensure", {"title": title})
            api("PUT", f"/novels/{NOVEL_ID}/knowledge", knowledge_data)
            api("POST", f"/novels/{NOVEL_ID}/manuscript/props", {
                "name": "铜钥匙", "description": "柄上刻有红色礁门记号，可开启九十二号仓库。",
                "holder_character_id": identity("lin"), "first_chapter": 1, "aliases": ["钥匙"]})
            api("POST", f"/novels/{NOVEL_ID}/foreshadow-ledger", {
                "entry_id": identity("question-1"), "chapter": 1,
                "character_id": identity("lin"), "question": "停航三年的灯塔为何仍在夜里亮灯？"})
            for act_number, act_title in enumerate(("来信与仓库", "礁洞与旧航线", "真相与归港"), 1):
                act = api("POST", f"/novels/{NOVEL_ID}/structure/nodes", {
                    "node_type": "act", "number": act_number, "title": act_title,
                    "description": "两章推进一个调查阶段；每阶段兑现一条线索。"})["node"]
                for chapter_number in (act_number * 2 - 1, act_number * 2):
                    api("POST", f"/novels/{NOVEL_ID}/structure/nodes", {
                        "node_type": "chapter", "number": chapter_number,
                        "title": CHAPTERS[chapter_number - 1][0], "parent_id": act["id"]})
            api("PUT", f"/novels/{NOVEL_ID}", {"generation_prefs": {"phase_display_mode": False}})
            api("PUT", f"/novels/{NOVEL_ID}/chapters/1/review", {
                "status": "reviewed", "memo": "人工测试样稿：请检查铜钥匙与仓库号的一致性。"})
            api("POST", f"/novels/{NOVEL_ID}/snapshots", {
                "trigger_type": "MANUAL", "name": "功能测试初始快照",
                "description": "自编样稿、设定与线索的起始保存点。"})
            for number in (1, 2):
                api("POST", f"/novels/{NOVEL_ID}/chapters/{number}/entity-mentions/reindex")
            installed = api("GET", f"/novels/{NOVEL_ID}/chapters")
            if [chapter["number"] for chapter in installed] != list(range(1, 7)):
                raise ValueError("Sample chapter verification failed; existing content preserved.")
            receipt = {"status": "installed", "novel_id": NOVEL_ID, "source": "self-authored",
                       "chapter_count": 6, "completed_chapters": 2, "model_invocations": 0}
            temporary = receipt_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(receipt_path)
            print("Sample installed: 2 written chapters, 4 pending chapters; no AI calls or model profiles imported.")
            return True
    finally:
        db.close_all()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="Explicit TaleWeft data directory; close its running instance first.")
    args = parser.parse_args(argv)
    try:
        install(args.data_dir)
    except Exception as exc:
        print(f"Sample installation stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
