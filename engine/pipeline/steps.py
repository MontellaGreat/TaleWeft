"""Step Result — 单步骤的执行结果

每个 _step_xxx() 返回 StepResult，管线根据它决定是否继续。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


RESULT_STATUSES = frozenset({"passed", "failed", "unavailable", "skipped", "deferred"})


def aggregate_result_status(statuses: List[str], *, success: bool = True) -> str:
    """Project execution evidence without treating an allowed degradation as a pass."""
    normalized = {"passed" if value == "ok" else value for value in statuses}
    if "failed" in normalized:
        return "failed"
    if "unavailable" in normalized or "warning" in normalized:
        return "unavailable"
    if not success:
        return "failed"
    if "deferred" in normalized:
        return "deferred"
    if normalized and normalized <= {"skipped"}:
        return "skipped"
    return "passed"


@dataclass
class StepResult:
    """单步骤执行结果

    - passed=True  → 继续管线
    - passed=False → 短路：停止管线（但已完成步骤的副作用不回滚）
    - skip=True    → 跳过当前步骤，继续管线
    """
    passed: bool = True
    skip: bool = False
    message: str = ""
    score: Optional[float] = None
    violations: List[Dict[str, Any]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "skipped" if self.skip else ("passed" if self.passed else "failed")
        if self.status not in RESULT_STATUSES:
            raise ValueError(f"Unknown pipeline result status: {self.status}")
        # Keep legacy skipped-step continuation semantics, but never label an
        # unavailable or deferred check as a successful check.
        self.passed = self.status in {"passed", "skipped"}
        self.skip = self.status == "skipped"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "skip": self.skip,
            "message": self.message,
            "score": self.score,
            "violations": list(self.violations),
            "suggestions": list(self.suggestions),
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def ok(message: str = "") -> "StepResult":
        return StepResult(passed=True, message=message)

    @staticmethod
    def fail(message: str, score: float = 0.0,
             violations: List[Dict[str, Any]] = None) -> "StepResult":
        return StepResult(
            passed=False, message=message, score=score,
            violations=violations or [],
        )

    @staticmethod
    def skip_step(reason: str = "") -> "StepResult":
        return StepResult(skip=True, message=reason)

    @staticmethod
    def unavailable(reason: str = "") -> "StepResult":
        return StepResult(status="unavailable", message=reason)

    @staticmethod
    def deferred(reason: str = "") -> "StepResult":
        return StepResult(status="deferred", message=reason)
