# Modified for TaleWeft on 2026-10-22: new QA identity; historical receipts still accepted.
"""Build an installer with a registry and runtime identity reserved for QA.

The regular production identity must never be used for installer verification:
NSIS maintenance mode keys on product name/publisher, and Tauri's single
instance plugin keys on the bundle identifier. This helper writes a separate
Tauri config next to the checked-in config and invokes the project's normal
build command. It does not touch HKCU/HKLM and does not launch the result.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
BASE_CONFIG = FRONTEND / "src-tauri" / "tauri.conf.json"


def validate_verification_artifact(path: Path, product: str) -> dict:
    """Bind a workspace build receipt to the exact QA installer before running it."""
    receipt = json.loads(path.with_suffix(path.suffix + ".verification.json").read_text(encoding="utf-8"))
    families = (("TaleWeft-Verify-", "TaleWeft Verification", "com.taleweft.verification."),
                ("PlotPilot-Verify-", "PlotPilot Verification", "com.plotpilot.verification."))
    family = next((entry for entry in families if product.startswith(entry[0])), None)
    if (family is None or receipt.get("product") != product
            or receipt.get("publisher") != family[1]
            or not str(receipt.get("identifier", "")).startswith(family[2])):
        raise ValueError("Installer identity is not the requested isolated verification identity")
    if hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get("sha256"):
        raise ValueError("Installer bytes differ from the verification build receipt")
    if not receipt.get("backend_build_id"):
        raise ValueError("Verification receipt has no backend build identity")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suffix", required=True,
                        help="unique verification identity suffix, e.g. 20261013")
    parser.add_argument("--config-out", type=Path,
                        default=FRONTEND / "src-tauri" / "tauri.verification.json")
    parser.add_argument("--target", default=None)
    parser.add_argument("--runner", type=Path,
                        help="optional Cargo-compatible runner, e.g. cargo-xwin for an isolated MSVC build")
    parser.add_argument("--bundles", default="nsis")
    parser.add_argument("--backend-resource", type=Path,
                        help="workspace-owned frozen backend for an isolated rollback baseline")
    parser.add_argument("--artifact-out", type=Path,
                        help="preserve the built QA installer and its identity/hash receipt")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    suffix = args.suffix.strip()
    if not suffix or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                         for ch in suffix):
        raise SystemExit("--suffix must be an alphanumeric isolated identity suffix")
    if suffix.lower() in {"taleweft", "plotpilot", "production", "prod"}:
        raise SystemExit("a production identity suffix is not allowed")

    config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    verify = copy.deepcopy(config)
    verify["productName"] = f"TaleWeft-Verify-{suffix}"
    verify["identifier"] = f"com.taleweft.verification.{suffix.lower().replace('-', '.') }"
    verify["bundle"]["publisher"] = "TaleWeft Verification"
    verify["bundle"]["shortDescription"] = "TaleWeft isolated installer verification"
    verify["bundle"]["longDescription"] = "Workspace-owned installer verification build"
    # Verification runs without elevation in a project-owned data directory;
    # production packaging keeps its checked-in install policy separately.
    verify["bundle"]["windows"]["nsis"]["installMode"] = "currentUser"
    # Keep the checked-in template and generated frontend resources identical.
    verify["bundle"]["windows"]["nsis"]["template"] = "nsis/installer.nsi"
    backend = (args.backend_resource or ROOT / "out/tauri/plotpilot-backend").resolve()
    if not backend.is_relative_to(ROOT.resolve()) or not (backend / "plotpilot-backend.exe").is_file():
        raise SystemExit("QA backend must be an existing frozen backend inside this workspace")
    if args.backend_resource:
        # Tauri applies an RFC 7396 merge patch. Remove the normal resource
        # entries so the rollback baseline cannot accidentally bundle both.
        verify["bundle"]["resources"] = {
            **{key: None for key in config["bundle"]["resources"]},
            backend.as_posix() + "/": "plotpilot-backend/",
        }
    backend_manifest = json.loads((backend / "_internal/release-manifest.json").read_text(encoding="utf-8"))
    out = args.config_out.resolve()
    if not out.is_relative_to(ROOT.resolve()):
        raise SystemExit("QA config must remain inside this workspace")
    out.write_text(json.dumps(verify, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
    if not npm:
        raise SystemExit("npm is unavailable; install the supported Node.js runtime first")
    command = [npm, "run", "tauri", "--", "build", "--config", str(out)]
    if args.runner:
        runner = args.runner.resolve()
        if not runner.is_file():
            raise SystemExit("--runner must point to an existing Cargo-compatible executable")
        command.extend(["--runner", str(runner)])
    if args.target:
        command.extend(["--target", args.target])
    if args.bundles:
        command.extend(["--bundles", args.bundles])
    print(json.dumps({"identity": verify["productName"], "identifier": verify["identifier"],
                      "publisher": verify["bundle"]["publisher"], "config": str(out),
                      "command": command}, ensure_ascii=False))
    if args.dry_run:
        return 0
    result = subprocess.run(command, cwd=FRONTEND, check=False).returncode
    if result:
        return result
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", FRONTEND / "src-tauri/target"))
    if args.target:
        target_dir /= args.target
    installers = list((target_dir / "release/bundle/nsis").glob(f"{verify['productName']}_*-setup.exe"))
    if not installers:
        raise SystemExit("Build succeeded without the expected QA installer artifact")
    artifact = max(installers, key=lambda path: path.stat().st_mtime_ns)
    if args.artifact_out:
        destination = args.artifact_out.resolve()
        if not destination.is_relative_to(ROOT.resolve()):
            raise SystemExit("QA artifacts must remain inside the workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, destination)
        artifact = destination
    receipt = {"product": verify["productName"], "publisher": verify["bundle"]["publisher"],
               "identifier": verify["identifier"], "backend_build_id": backend_manifest["build_id"],
               "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "config": str(out)}
    artifact.with_suffix(artifact.suffix + ".verification.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(artifact), **receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
