"""An old daemon must not work using another run's running projection."""
import os
from types import SimpleNamespace

import pytest

from application.engine.services import autopilot_control as control
from application.engine.services import execution_lease as leases
from application.engine.services.novel_stop_signal import clear_all_stop_signals
from domain.novel.entities.novel import AutopilotStatus
from domain.novel.value_objects.novel_id import NovelId
from engine.runtime.daemon_host import DaemonHostMixin
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


@pytest.fixture
def execution(tmp_path, monkeypatch):
    with sqlite_writes_bypass_queue():
        db = DatabaseConnection(str(tmp_path / 'ownership.sqlite'))
        db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','book','book')")
        db.commit()
        clear_all_stop_signals()
        control._processing.clear()
        monkeypatch.setattr(control, '_db', lambda database=None: database or db)
        host = DaemonHostMixin()
        host._read_autopilot_status_ephemeral = lambda nid: AutopilotStatus(db.fetch_one(
            'SELECT autopilot_status FROM novels WHERE id=?', (nid.value,))['autopilot_status'])
        novel = SimpleNamespace(novel_id=NovelId('book'))
        yield db, host, novel
        clear_all_stop_signals()
        control._processing.clear()
        db.close_all(skip_checkpoint=True)


def start(db):
    command = control.submit_command('book', 'start', {'next_stage':'macro_planning',
        'max_auto_chapters':10, 'target_chapters':10, 'target_words_per_chapter':2000}, db=db)
    control.process_pending_commands(db=db)
    return command


def test_running_without_bound_identity_cannot_authorize_work(execution):
    db, host, novel = execution
    start(db)
    assert not host._is_still_running(novel)
    assert host._begin_novel_execution(novel.novel_id)
    assert host._is_still_running(novel)
    assert host._novel_is_running(novel.novel_id)
    host._end_novel_execution(novel.novel_id)
    assert not host._novel_is_running_in_db(novel.novel_id)


@pytest.mark.parametrize('mutation', ['expired','no_lease','dag','job','epoch','pid','stopped'])
def test_each_lost_authority_stops_daemon_even_if_projection_running(execution, mutation):
    db, host, novel = execution
    command = start(db)
    assert host._begin_novel_execution(novel.novel_id)
    if mutation == 'expired':
        db.execute('UPDATE novel_execution_leases SET expires_at=0')
    elif mutation == 'no_lease':
        db.execute('DELETE FROM novel_execution_leases')
    elif mutation == 'dag':
        db.execute("UPDATE novel_execution_leases SET owner='dag',job_id='successor'")
    elif mutation == 'job':
        db.execute("UPDATE novel_execution_leases SET job_id='different'")
    elif mutation == 'epoch':
        db.execute('UPDATE novels SET autopilot_run_epoch=autopilot_run_epoch+1')
    elif mutation == 'pid':
        db.execute('UPDATE autopilot_commands SET executor_pid=?', (os.getpid()+1,))
    elif mutation == 'stopped':
        control.mark_processing('book')
        control.submit_command('book', 'stop', {}, db=db)
    db.commit()
    assert not host._is_still_running(novel)
    assert not host._novel_is_running(novel.novel_id)
    assert host._execution_identity(novel.novel_id)['job_id'] == command['command_id']


def test_running_legacy_row_without_command_is_denied(execution):
    db, host, novel = execution
    db.execute("UPDATE novels SET autopilot_status='running'")
    leases.claim_execution_lease('book','autopilot','legacy',db=db)
    control.ensure_control_schema(db)
    assert not host._begin_novel_execution(novel.novel_id)


def test_ownership_read_error_fails_closed(execution, monkeypatch):
    db, host, novel = execution
    start(db)
    assert host._begin_novel_execution(novel.novel_id)
    def broken(*args, **kwargs):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(control, 'executor_identity', broken)
    assert not host._is_still_running(novel)
    assert not host._begin_novel_execution(novel.novel_id)
