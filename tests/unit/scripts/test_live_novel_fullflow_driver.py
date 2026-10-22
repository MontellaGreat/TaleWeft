"""Acceptance-driver safety: bounded paid retries and truthful cascade results."""
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('live_fullflow_driver_under_test', ROOT / 'scripts/verification/live_novel_fullflow.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class InvocationClient:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.posts = []

    def json(self, route, method='GET', payload=None):
        if method == 'POST':
            self.posts.append((route, payload))
            return {'session': {'status': 'generating'}}
        status = next(self.statuses)
        return {'session': {'status': status}, 'commit': {'status': 'succeeded'}}


def poll_without_wait(callback, _label):
    for _ in range(10):
        result = callback()
        if result is not None:
            return result
    raise AssertionError('Driver did not reach a terminal result')


def test_failed_invocation_retry_is_bound_to_one_dispatch_and_survives_resume(tmp_path):
    client = InvocationClient(['failed', 'generating', 'completed'])
    run = driver.Run(tmp_path, client, 60, retry_failed=True)
    run.stage, run.poll = 'worldbuilding', poll_without_wait
    result = run.invocation('sid')
    assert result['explicit_retries'] == 1
    assert [r[0] for r in client.posts] == ['/api/v1/ai-invocations/sid/retry']
    # A subsequent process reads the same checkpoint. It must not silently
    # spend a second retry after a failed first retry or ambiguous disconnect.
    later_client = InvocationClient(['failed'])
    later = driver.Run(tmp_path, later_client, 60, retry_failed=True)
    later.poll = poll_without_wait
    with pytest.raises(driver.CheckFailed, match='retry limit'):
        later.invocation('sid')
    assert later_client.posts == []


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'blocked'])
def test_driver_never_retries_without_explicit_retry_option(tmp_path, status):
    client = InvocationClient([status])
    run = driver.Run(tmp_path, client, 60)
    run.poll = poll_without_wait
    with pytest.raises(driver.CheckFailed):
        run.invocation('sid')
    assert client.posts == []


def test_local_job_completion_does_not_mask_failed_downstream_reconciliation(tmp_path):
    run = driver.Run(tmp_path, None, 60)
    states = iter(['running', 'unavailable'])
    run.poll = poll_without_wait
    run.jobs = lambda _number: [{'job_id': 'job', 'revision': 2, 'status': 'completed',
        'attempts': 1, 'cascade_status': next(states)}]
    with pytest.raises(driver.CheckFailed, match='downstream reconciliation'):
        run.wait_job(1, 2, require_cascade=True)


def test_driver_waits_for_both_local_and_downstream_completion(tmp_path):
    run = driver.Run(tmp_path, None, 60)
    states = iter(['running', 'completed'])
    run.poll = poll_without_wait
    run.jobs = lambda _number: [{'job_id': 'job', 'revision': 2, 'status': 'completed',
        'attempts': 1, 'cascade_status': next(states)}]
    assert run.wait_job(1, 2, require_cascade=True)['cascade_status'] == 'completed'


def test_downstream_semantic_report_preserves_old_later_prose_and_exposes_review_need():
    evidence = driver.semantic_review_evidence('林雁仍拿着编号七十四的钥匙。', [
        {'chapter_number': 1, 'status': 'candidate', 'payload_json': '{"钥匙":"九十二"}', 'text_span': ''},
        {'chapter_number': 2, 'status': 'candidate', 'payload_json': '{"钥匙":"七十四"}', 'text_span': ''},
    ], {'status': 'active', 'conflicts_json': '[]'}, [], None)
    assert evidence['chapter_2_prose_mentions_obsolete_number'] is True
    assert evidence['chapter_2_active_automatic_memory']['old_number_records'] == 1
    assert evidence['cross_chapter_active_automatic_memory_has_both_numbers'] is True
    assert evidence['full_book_consistency'] == 'requires_author_review'
    assert evidence['automatic_semantic_correction_verified'] is False


def test_clean_reanalysis_alone_is_not_proof_of_full_book_semantic_consistency():
    evidence = driver.semantic_review_evidence('他们核对了账簿。', [
        {'chapter_number': 1, 'status': 'candidate', 'payload_json': '九十二', 'text_span': ''},
        {'chapter_number': 2, 'status': 'superseded', 'payload_json': '七十四', 'text_span': ''},
    ], {'status': 'active', 'conflicts_json': '[]'}, [], {'status': 'draft', 'memo': ''})
    assert evidence['chapter_2_prose_mentions_obsolete_number'] is False
    assert evidence['chapter_2_active_automatic_memory']['old_number_records'] == 0
    assert evidence['full_book_consistency'] == 'not_established_by_reanalysis'
    assert evidence['automatic_semantic_correction_verified'] is False
