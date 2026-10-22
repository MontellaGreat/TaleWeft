# Modified for TaleWeft on 2026-10-22: independent branding; upstream attribution retained.
import hashlib
import json

import pytest

from scripts.build_verification_installer import validate_verification_artifact


def receipt(tmp_path, **overrides):
    artifact = tmp_path / "qa.exe"
    artifact.write_bytes(b"isolated QA artifact")
    value = {"product": "PlotPilot-Verify-unit", "publisher": "PlotPilot Verification",
             "identifier": "com.plotpilot.verification.unit", "backend_build_id": "source-test",
             "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), **overrides}
    artifact.with_suffix(".exe.verification.json").write_text(json.dumps(value), encoding="utf-8")
    return artifact


def test_accepts_exact_workspace_qa_identity(tmp_path):
    assert validate_verification_artifact(receipt(tmp_path), "PlotPilot-Verify-unit")["backend_build_id"] == "source-test"


@pytest.mark.parametrize("identity", [
    {"product": "PlotPilot"}, {"publisher": "PlotPilot Team"},
    {"identifier": "com.plotpilot.desktop"}, {"backend_build_id": ""},
])
def test_rejects_production_or_incomplete_receipt(tmp_path, identity):
    with pytest.raises(ValueError):
        validate_verification_artifact(receipt(tmp_path, **identity), "PlotPilot-Verify-unit")


def test_rejects_replaced_installer(tmp_path):
    artifact = receipt(tmp_path)
    artifact.write_bytes(b"another executable")
    with pytest.raises(ValueError, match="bytes differ"):
        validate_verification_artifact(artifact, "PlotPilot-Verify-unit")


def test_accepts_new_qa_identity(tmp_path):
    artifact = receipt(tmp_path, product="TaleWeft-Verify-unit", publisher="TaleWeft Verification",
                       identifier="com.taleweft.verification.unit")
    assert validate_verification_artifact(artifact, "TaleWeft-Verify-unit")["publisher"] == "TaleWeft Verification"


@pytest.mark.parametrize("override", [
    {"product": "TaleWeft"}, {"publisher": "TaleWeft"},
    {"publisher": "PlotPilot Verification"}, {"identifier": "com.taleweft.desktop"},
])
def test_new_qa_receipt_rejects_production_and_mixed_identities(tmp_path, override):
    metadata = {"product": "TaleWeft-Verify-unit", "publisher": "TaleWeft Verification",
                "identifier": "com.taleweft.verification.unit", **override}
    with pytest.raises(ValueError):
        validate_verification_artifact(receipt(tmp_path, **metadata), "TaleWeft-Verify-unit")
