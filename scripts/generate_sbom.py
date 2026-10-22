# Modified for TaleWeft on 2026-10-22: SBOM product metadata.
"""Generate a dependency SBOM from the locked Python/npm/Cargo manifests.

The script intentionally uses lockfile metadata only, so CI does not need to
download or execute a third-party SBOM tool. It emits CycloneDX 1.5 JSON with
components and package URLs for the three runtime ecosystems.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def _python_components(path: Path):
    text = path.read_text(encoding="utf-8")
    current = None
    version = None
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ]+)", line)
        if match:
            current, version = match.groups()
        elif current and "--hash=" in line:
            yield {
                "type": "library", "name": current, "version": version,
                "purl": f"pkg:pypi/{quote(current)}@{quote(version)}",
            }
            current = version = None


def _npm_components(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = data.get("packages", {})
    for install_path, info in packages.items():
        if not install_path or not info.get("version"):
            continue
        name = info.get('name') or install_path.rsplit('node_modules/', 1)[-1]
        component = {
            "type": "library", "name": name,
            "version": info["version"],
            "purl": f"pkg:npm/{quote(name, safe='/')}@{quote(info['version'])}",
            "scope": "optional" if info.get('dev') else "required",
        }
        if info.get("license"):
            component["licenses"] = [{"expression": info["license"]}]
        yield component


def _cargo_components(path: Path):
    data = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n\[\[package\]\]\n", "\n" + data)
    for block in blocks[1:]:
        name = re.search(r"^name = \"([^\"]+)\"", block, re.M)
        version = re.search(r"^version = \"([^\"]+)\"", block, re.M)
        checksum = re.search(r"^checksum = \"([^\"]+)\"", block, re.M)
        if not name or not version:
            continue
        component = {
            "type": "library", "name": name.group(1), "version": version.group(1),
            "purl": f"pkg:cargo/{quote(name.group(1))}@{quote(version.group(1))}",
        }
        if checksum:
            component["hashes"] = [{"alg": "SHA-256", "content": checksum.group(1)}]
        yield component


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "analysis/implementation/sbom.cdx.json")
    args = parser.parse_args()
    locks = ['requirements.lock', 'requirements-build.lock', 'requirements-architecture.lock',
             'frontend/package-lock.json', 'frontend/src-tauri/Cargo.lock']
    components = list(_python_components(ROOT / "requirements.lock"))
    for lock in ('requirements-build.lock', 'requirements-architecture.lock'):
        components.extend(dict(component, scope='optional') for component in _python_components(ROOT / lock))
    components.extend(_npm_components(ROOT / "frontend/package-lock.json"))
    components.extend(_cargo_components(ROOT / "frontend/src-tauri/Cargo.lock"))
    unique = {}
    for component in components:
        component['bom-ref'] = component['purl']
        # Runtime requirements precede tooling so optional repeats cannot
        # relabel runtime dependencies. Nested npm installs share one identity.
        unique.setdefault(component['purl'], component)
    components = sorted(unique.values(), key=lambda component: component['purl'])
    sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {"type": "application", "name": "TaleWeft", "version": json.loads((ROOT / 'release-manifest.json').read_text(encoding='utf-8'))['version']},
                     "properties": [
                         {"name": 'taleweft:scope', "value": 'locked dependencies, including optional build/test tooling; not binary inventory'},
                         *[{"name": 'taleweft:lock-sha256:' + lock,
                            "value": hashlib.sha256((ROOT / lock).read_bytes()).hexdigest()} for lock in locks],
                     ]},
        "components": components,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(components)} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
