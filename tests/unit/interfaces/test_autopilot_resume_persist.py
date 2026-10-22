from types import SimpleNamespace

from domain.novel.entities.novel import AutopilotStatus, NovelStage
from interfaces.api.v1.engine import autopilot_routes


class _Repo:
    def __init__(self):
        self.patches = []

    def patch(self, novel_id, **fields):
        self.patches.append((novel_id.value, fields))


def test_resume_persist_keeps_explicit_next_stage(monkeypatch):
    repo = _Repo()

    captured = {}
    def submit(*args, **kwargs):
        captured.update(kwargs)
        return {"decision": SimpleNamespace(next_stage="paused_for_review"), "run_epoch": 7}
    monkeypatch.setattr(autopilot_routes, "_persist_autopilot_running_sync", submit)
    monkeypatch.setattr(autopilot_routes, "get_novel_repository", lambda: repo)

    result = autopilot_routes._persist_autopilot_resume_sync(
        "novel-1",
        next_stage=NovelStage.ACT_PLANNING.value,
        current_act=0,
        max_auto_chapters=9999,
        target_chapters=150,
        target_words_per_chapter=2000,
    )

    assert result["run_epoch"] == 7
    assert repo.patches == []  # API submission must not fabricate executor RUNNING.
    assert captured["next_stage_override"] == NovelStage.ACT_PLANNING.value
    assert captured["current_act_override"] == 0
