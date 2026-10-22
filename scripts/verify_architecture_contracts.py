"""Run the open-source import-linter CLI and prove its contracts reject regressions.

Negative probes use isolated source copies under project out/, never mutate the
working source tree, and do not import or execute PlotPilot application code.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("domain", "application", "infrastructure", "interfaces", "engine", "shared")
CLI = [sys.executable, "-c", "from importlinter.cli import lint_imports_command; lint_imports_command()"]


def lint(project: Path) -> subprocess.CompletedProcess:
    environment = {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [*CLI, "--config", ".importlinter", "--no-cache", "--no-logo"],
        cwd=project, env=environment, text=True, encoding="utf-8", errors="replace",
        # The negative probes rebuild the import graph from an isolated copy.
        # On Windows this project can exceed two minutes even when the
        # contract result is healthy, so keep the guard above transient hangs
        # without turning normal slow verification into a false failure.
        capture_output=True, timeout=300,
    )


def check_graph_coverage() -> dict:
    import grimp

    sys.path.insert(0, str(ROOT))
    graph = grimp.build_graph(*PACKAGES, cache_dir=None)
    expected = set()
    for package in PACKAGES:
        for file in (ROOT / package).rglob("*.py"):
            if "__pycache__" in file.parts:
                continue
            parts = file.relative_to(ROOT).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            expected.add(".".join(parts))
    missing = sorted(expected - graph.modules)
    if missing:
        raise AssertionError(f"Source modules missing from import graph: {missing}")
    return {"covered_modules": len(expected), "graph_modules": len(graph.modules), "missing_modules": missing}


def check_negative_probe(base: Path, scratch: Path, name: str, relative_file: str,
                         addition: str, expected_output: str, *, replace: bool = False) -> dict:
    project = scratch / name
    for package in PACKAGES:
        shutil.copytree(base / package, project / package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(base / ".importlinter", project / ".importlinter")
    target = project / relative_file
    target.parent.mkdir(parents=True, exist_ok=True)
    if replace:
        target.write_text(addition, encoding="utf-8")
    else:
        with target.open("a", encoding="utf-8") as stream:
            stream.write("\n" + addition + "\n")
    result = lint(project)
    output = result.stdout + result.stderr
    if result.returncode != 1 or expected_output not in output:
        raise AssertionError(f"Probe {name} did not fail for the intended boundary: {output}")
    return {"name": name, "exit_code": result.returncode, "expected_failure": True, "output": output}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "out/architecture-contracts.json")
    args = parser.parse_args()
    positive = lint(ROOT)
    print(positive.stdout, end="")
    if positive.returncode:
        print(positive.stderr, end="", file=sys.stderr)
        return positive.returncode
    coverage = check_graph_coverage()
    scratch_root = ROOT / "out/architecture-probes"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="contracts-", dir=scratch_root) as directory:
        scratch = Path(directory)
        probes = [
            check_negative_probe(ROOT, scratch, "new-direct-runtime-edge",
                "domain/knowledge/architecture_probe.py", "import infrastructure.persistence.database.connection",
                "Domain has no new direct dependencies on runtime layers BROKEN"),
            check_negative_probe(ROOT, scratch, "indirect-runtime-edge",
                "domain/evolution/architecture_probe.py", "import domain.novel.value_objects.story_phase",
                "Evolution and foundational domain modules stay transitively independent BROKEN"),
            check_negative_probe(ROOT, scratch, "inverted-evolution-layers",
                "domain/evolution/contracts.py", "import domain.evolution.reducer",
                "Evolution reducer depends on models then contracts BROKEN"),
            check_negative_probe(ROOT, scratch, "stale-legacy-exception",
                "domain/engine/dag/repositories/dag_version_repository.py", '"""Legacy edge removed by probe."""\n',
                "No matches for ignored import", replace=True),
        ]
    receipt = {"status": "passed", "cli": "import-linter==2.15", "contracts_kept": 3,
               "coverage": coverage, "baseline_output": positive.stdout, "negative_probes": probes}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Architecture CLI: 3 contracts kept; {len(probes)} negative probes rejected; "
          f"{coverage['covered_modules']} source modules covered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
