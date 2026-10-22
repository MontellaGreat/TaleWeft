from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from engine.runtime.policy_validator import PolicyUnavailableError, PolicyValidator, PolicyViolationError


def _validator(guardrail):
    validator = PolicyValidator.__new__(PolicyValidator)
    validator._guardrail = guardrail
    validator._initialization_error = "checker unavailable"
    validator._min_pass_score = 0.6
    return validator


@pytest.mark.parametrize("guardrail", [None, SimpleNamespace(check=Mock(side_effect=RuntimeError("checker crashed")))])
def test_check_and_advise_report_unavailable_without_manufacturing_a_pass(guardrail):
    report = _validator(guardrail).advise("正文")
    assert report.status == "unavailable" and report.passed is False
    assert report.overall_score == 0.0
    assert report.to_dict()["status"] == "unavailable"
    assert report.all_violations[0]["type"] == "validator_unavailable"


@pytest.mark.parametrize("guardrail", [None, SimpleNamespace(check=Mock(side_effect=RuntimeError("checker crashed")))])
def test_enforce_rejects_unavailable_checker_even_with_zero_threshold(guardrail):
    with pytest.raises(PolicyUnavailableError):
        _validator(guardrail).enforce("正文", min_score=0.0)


@pytest.mark.parametrize("passed", [True, False])
def test_normal_reports_keep_real_scores_and_hard_failure_cannot_be_bypassed(passed):
    real = SimpleNamespace(overall_score=0.9, language_style_score=0.9, character_consistency_score=0.9,
                           plot_density_score=0.9, naming_score=0.9, viewpoint_score=0.9, rhythm_score=0.9,
                           all_violations=[], passed=passed)
    validator = _validator(SimpleNamespace(check=Mock(return_value=real)))
    report = validator.check("正文")
    assert report.overall_score == 0.9 and report.passed is passed
    assert report.status == ("passed" if passed else "failed")
    if passed:
        assert validator.enforce("正文").passed is True
    else:
        with pytest.raises(PolicyViolationError):
            validator.enforce("正文")
