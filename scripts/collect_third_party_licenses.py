"""Collect release notices from installed, locked upstream packages.

Run in the packaging venv after npm ci and cargo fetch. No user data is read.
The inventory deliberately includes Python runtime locks, frontend production
dependencies and the Windows Cargo normal/build tree, not every installed tool.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from urllib.error import URLError
from packaging.markers import Marker
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ("license", "licence", "copying", "copyright", "notice", "authors")
# Some npm tarballs omit LICENSE even though their source repositories carry
# it. Pin URLs to tags/paths verified for the locked package version.
UPSTREAM_NOTICES = {
    "@css-render/plugin-bem": "https://raw.githubusercontent.com/07akioni/css-render/v0.15.14/LICENSE",
    "@css-render/vue3-ssr": "https://raw.githubusercontent.com/07akioni/css-render/v0.15.14/LICENSE",
    "css-render": "https://raw.githubusercontent.com/07akioni/css-render/v0.15.14/LICENSE",
    "agent-base": "https://raw.githubusercontent.com/TooTallNate/proxy-agents/main/packages/agent-base/LICENSE",
    "https-proxy-agent": "https://raw.githubusercontent.com/TooTallNate/proxy-agents/main/packages/https-proxy-agent/LICENSE",
    "vdirs": "https://raw.githubusercontent.com/07akioni/css-render/v0.15.14/LICENSE",
}
VERSIONED_UPSTREAM_NOTICES = {
    ("@vue/devtools-api", "6.6.4"): "https://raw.githubusercontent.com/vuejs/devtools-v6/v6.6.4/LICENSE",
    ("@vue/devtools-api", "7.7.9"): "https://raw.githubusercontent.com/vuejs/devtools/v7.7.9/LICENSE",
}
ADDITIONAL_NOTICES = {
    # @vicons/ionicons5 bundles vector assets derived from Ionicons.
    "@vicons/ionicons5": "https://raw.githubusercontent.com/ionic-team/ionicons/main/LICENSE",
}
CARGO_NOTICE_URLS = {
    "alloc-stdlib": "https://raw.githubusercontent.com/dropbox/rust-alloc-no-stdlib/master/LICENSE",
    "selectors": "https://www.mozilla.org/media/MPL/2.0/index.txt",
    "webview2-com": "https://raw.githubusercontent.com/wravery/webview2-rs/main/LICENSE",
    "webview2-com-macros": "https://raw.githubusercontent.com/wravery/webview2-rs/main/LICENSE",
    "webview2-com-sys": "https://raw.githubusercontent.com/wravery/webview2-rs/main/LICENSE",
}
NATIVE_SOURCE_ARCHIVES = [
    {
        "name": "libiconv-winlibs",
        "version": "accac417318a3eef7402685a268cab8037e62ba1",
        "url": "https://codeload.github.com/winlibs/libiconv/tar.gz/accac417318a3eef7402685a268cab8037e62ba1",
        "license": "LGPL-2.1-or-later",
    },
    {
        "name": "libxml2-winlibs",
        "version": "bb84678855e3407fc88180295c992f9c984e28e0",
        "url": "https://codeload.github.com/winlibs/libxml2/tar.gz/bb84678855e3407fc88180295c992f9c984e28e0",
        "license": "MIT",
    },
    {
        "name": "libxslt-winlibs",
        "version": "c0076eaa6e0d1f99e01107e8db225a308eb1e543",
        "url": "https://codeload.github.com/winlibs/libxslt/tar.gz/c0076eaa6e0d1f99e01107e8db225a308eb1e543",
        "license": "MIT",
    },
    {
        "name": "zlib-winlibs",
        "version": "0089522e62e6b5d4950e62bd20f0cb06a9288413",
        "url": "https://codeload.github.com/winlibs/zlib/tar.gz/0089522e62e6b5d4950e62bd20f0cb06a9288413",
        "license": "Zlib",
    },
]
NATIVE_RECIPE_FILES = {
    "build.ps1": "https://raw.githubusercontent.com/lxml/libxml2-win-binaries/2026.10.05/build.ps1",
    "libiconv.patch": "https://raw.githubusercontent.com/lxml/libxml2-win-binaries/2026.10.05/libiconv.patch",
    "gitmodules.txt": "https://raw.githubusercontent.com/lxml/libxml2-win-binaries/2026.10.05/.gitmodules",
    "workflow-main.yml": "https://raw.githubusercontent.com/lxml/libxml2-win-binaries/2026.10.05/.github/workflows/main.yml",
}
LXML_WINDOWS_TAG = "2026.10.05"
LXML_WINDOWS_TAG_OBJECT = "300d69d1031f3c530cfb0e5bda02d80049b6e9e6"
LXML_WINDOWS_PEELED_COMMIT = "bed89b4f0819fa3a7bf456d55780ee7a4ebb325e"
LXML_WINDOWS_ASSETS = [
    {"name": "iconv-1.19.win64.zip", "sha256": "4a92826a127789941ce920eeb6cc4289e40e241bf2bcab3067b00246dce608b5"},
    {"name": "libxml2-2.11.9.win64.zip", "sha256": "86dacde1187a9bdf46d12649ba1f7bcd00a041dd60e9461b033ea8546ee4d932"},
    {"name": "libxslt-1.1.45.win64.zip", "sha256": "32764ee260d1b5991afd2d1c49b133de31cebbc49c839e49316f029795043534"},
    {"name": "zlib-1.3.2.win64.zip", "sha256": "f78a02794a598afbe09ad7523be69dbd019665626562af798bf0ce971262ffb3"},
]
LXML_WINDOWS_SUBMODULES = [
    {"name": "libiconv", "repository": "https://github.com/winlibs/libiconv", "commit": "accac417318a3eef7402685a268cab8037e62ba1", "source": "sources/native/libiconv-winlibs-accac417318a3eef7402685a268cab8037e62ba1.tar.gz"},
    {"name": "libxml2", "repository": "https://github.com/winlibs/libxml2", "commit": "bb84678855e3407fc88180295c992f9c984e28e0", "source": "sources/native/libxml2-winlibs-bb84678855e3407fc88180295c992f9c984e28e0.tar.gz"},
    {"name": "libxslt", "repository": "https://github.com/winlibs/libxslt", "commit": "c0076eaa6e0d1f99e01107e8db225a308eb1e543", "source": "sources/native/libxslt-winlibs-c0076eaa6e0d1f99e01107e8db225a308eb1e543.tar.gz"},
    {"name": "zlib", "repository": "https://github.com/winlibs/zlib", "commit": "0089522e62e6b5d4950e62bd20f0cb06a9288413", "source": "sources/native/zlib-winlibs-0089522e62e6b5d4950e62bd20f0cb06a9288413.tar.gz"},
]


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.+-]", "_", name)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request(url: str) -> bytes:
    last_error = None
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"User-Agent": "TaleWeft-license-collector"}), timeout=120) as response:
                return response.read()
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise last_error


def fetch(url: str, destination: Path, expected: str | None = None) -> Path:
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = request(url)
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Hash mismatch downloading {destination.name}")
        destination.write_bytes(data)
    if expected and digest(destination) != expected:
        raise ValueError(f"Hash mismatch: {destination.name}")
    return destination


def notice_files(path: Path):
    """Include nested bundled-native notices while skipping source/build trees."""
    for child in sorted(path.iterdir()):
        if child.name.lower().startswith(PREFIXES):
            if child.is_file():
                yield child
            elif child.is_dir():
                yield from sorted(f for f in child.rglob("*") if f.is_file())


def copy_files(files, base: Path, destination: Path, output: Path) -> list[str]:
    copied = []
    for source in sorted(set(files)):
        relative = source.relative_to(base)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(target.relative_to(output).as_posix())
    return copied


def locked_python(path: Path) -> dict[str, str]:
    from packaging.markers import default_environment

    env = default_environment()
    versions = {}
    for line in path.read_text("utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(?:\s*;\s*(.*?))?\s*\\?$", line)
        if not match:
            continue
        name, version, marker = match.groups()
        if marker and not Marker(marker).evaluate(env):
            continue
        versions[canonical(name)] = version
    return versions


def python_components(output: Path):
    installed = {canonical(d.metadata["Name"]): d for d in metadata.distributions()}
    versions = locked_python(ROOT / "requirements.lock")
    # The bootloader is distributed, although PyInstaller itself is a build tool.
    versions["pyinstaller"] = locked_python(ROOT / "requirements-build.lock")["pyinstaller"]
    entries = []
    for name, version in sorted(versions.items()):
        dist = installed.get(name)
        entry = {"ecosystem": "python", "name": name, "version": version, "scope": "runtime-lock"}
        if dist is None or dist.version != version:
            entry["error"] = "locked version is not installed"
        else:
            info = dist.metadata
            entry["license"] = info.get("License-Expression") or info.get("License") or "see-original-text"
            classifiers = info.get_all("Classifier") or []
            entry["license_classifiers"] = [s for s in classifiers if s.startswith("License ::")]
            entry["upstream"] = info.get_all("Project-URL") or [info.get("Home-page", "")]
            base = Path(dist.locate_file(""))
            files = [Path(dist.locate_file(f)) for f in dist.files or []
                     if any(Path(str(f)).name.lower().startswith(s) for s in PREFIXES)]
            files = [f for f in files if f.is_file() and f.is_relative_to(base)]
            entry["notices"] = copy_files(files, base, output / "python" / f"{name}-{version}", output)
            if not entry["notices"]:
                entry["error"] = "original license text missing"
        entries.append(entry)
    return entries


def npm_components(output: Path):
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text("utf-8"))
    entries = []
    seen = set()
    for relative, info in sorted(lock["packages"].items()):
        if not relative or info.get("dev"):
            continue
        name = info.get("name") or relative.rsplit("node_modules/", 1)[-1]
        identity = (name, info["version"])
        if identity in seen:
            continue
        seen.add(identity)
        base = ROOT / "frontend" / relative
        target = output / "npm" / f"{safe_name(name)}-{info['version']}"
        entry = {"ecosystem": "npm", "name": name, "version": info["version"],
                 "license": info.get("license", "UNKNOWN"), "scope": "production-lock"}
        if not base.is_dir():
            entry["error"] = "locked production package is not installed"
        else:
            entry["notices"] = copy_files(notice_files(base), base, target, output)
            url = VERSIONED_UPSTREAM_NOTICES.get((name, info["version"])) or UPSTREAM_NOTICES.get(name)
            if not entry["notices"] and url:
                target_file = fetch(url, target / "LICENSE.upstream")
                entry["notice_origin"] = url
                entry["notices"] = [target_file.relative_to(output).as_posix()]
            if name in ADDITIONAL_NOTICES:
                extra = fetch(ADDITIONAL_NOTICES[name], target / "LICENSE-Ionicons.upstream")
                entry.setdefault("notices", []).append(extra.relative_to(output).as_posix())
                entry["additional_notice_origin"] = ADDITIONAL_NOTICES[name]
            if not entry["notices"] and entry["license"] != "UNKNOWN":
                # Preserve the package metadata and the canonical SPDX text URL
                # when a package omits a verbatim license file. This is explicit
                # in inventory.json so a future release can replace it with the
                # upstream text without silently dropping the obligation.
                metadata_file = target / "package.json"
                metadata_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(base / "package.json", metadata_file)
                reference = target / "LICENSE-REFERENCE.txt"
                reference.write_text(
                    f"{name} {info['version']} declares {entry['license']} in package.json.\n"
                    f"Canonical text: https://spdx.org/licenses/{entry['license'].replace(' ', '%20')}.html\n",
                    encoding="utf-8",
                )
                entry["notices"] = [metadata_file.relative_to(output).as_posix(), reference.relative_to(output).as_posix()]
                entry["notice_origin"] = "package.json SPDX metadata; canonical text reference"
            if not entry["notices"]:
                entry["error"] = "original license text missing"
        entries.append(entry)
    return entries


def cargo_components(output: Path, cargo: str, registry: Path):
    command = [cargo, "tree", "--manifest-path", str(ROOT / "frontend/src-tauri/Cargo.toml"),
               "--locked", "--offline", "--target", "x86_64-pc-windows-gnu", "--edges", "normal,build",
               "--prefix", "none", "--format", "{p}", "--no-dedupe"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    identities = sorted(set(re.findall(r"^([A-Za-z0-9_-]+) v([^\s]+)", result.stdout, re.M)))
    lock = tomllib.loads((ROOT / "frontend/src-tauri/Cargo.lock").read_text("utf-8"))
    locked = {(p["name"], p["version"]): p for p in lock["package"] if "source" in p}
    entries = []
    for name, version in identities:
        if (name, version) not in locked:
            continue
        base = registry / f"{name}-{version}"
        package = tomllib.loads((base / "Cargo.toml").read_text("utf-8"))["package"]
        files = list(notice_files(base))
        # Native libraries vendored by -sys crates often carry additional notices.
        if name.endswith("-sys") or name in {"ring", "aws-lc-rs", "aws-lc-sys"}:
            files.extend(p for p in base.rglob("*") if p.is_file() and p.name.lower().startswith(PREFIXES))
        if package.get("license-file"):
            files.append(base / package["license-file"])
        entry = {"ecosystem": "cargo", "name": name, "version": version,
                 "license": package.get("license", "see-original-text"),
                 "upstream": package.get("repository", ""),
                 "scope": "windows-normal-and-build-tree", "checksum": locked[(name, version)].get("checksum"),
                 "notices": copy_files(files, base, output / "cargo" / f"{name}-{version}", output)}
        if not entry["notices"]:
            url = CARGO_NOTICE_URLS.get(name)
            if url:
                target = output / "cargo" / f"{name}-{version}" / "LICENSE.upstream"
                fetched = fetch(url, target)
                entry["notices"] = [fetched.relative_to(output).as_posix()]
                entry["notice_origin"] = url
            else:
                entry["error"] = "original license text missing"
        entries.append(entry)
    return entries


def runtime_notices(output: Path, rust_home: Path | None, mingw: Path | None):
    entries = []
    sources = [(Path(sys.base_prefix) / "LICENSE.txt", "CPython", sys.version.split()[0])]
    if rust_home:
        sources.append((rust_home / "share/doc/rust/COPYRIGHT-library.html", "Rust-standard-library", "installed-toolchain"))
    if mingw:
        sources.append((mingw / "COPYING.MinGW-w64-runtime.txt", "MinGW-w64-runtime", "installed-toolchain"))
    for source, name, version in sources:
        target = output / "runtime" / name / source.name
        if not source.is_file():
            entries.append({"name": name, "version": version, "ecosystem": "runtime", "error": "runtime notice missing"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        entries.append({"name": name, "version": version, "ecosystem": "runtime",
                        "notices": [target.relative_to(output).as_posix()]})
    return entries


def source_archive(output: Path, name: str, version: str):
    data = json.loads(request(f"https://pypi.org/pypi/{name}/{version}/json"))
    archive = next(item for item in data["urls"] if item["packagetype"] == "sdist")
    path = fetch(archive["url"], output / "sources" / archive["filename"], archive["digests"]["sha256"])
    return {"name": name, "version": version, "file": path.relative_to(output).as_posix(),
            "url": archive["url"], "sha256": digest(path)}


def collect_sources(output: Path, entries: list[dict], cache: Path):
    sources = []
    required = {"fpdf2", "certifi", "lxml", "cryptography"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(source_archive, output, e["name"], e["version"])
                   for e in entries if e["ecosystem"] == "python" and e["name"] in required]
        sources.extend(f.result() for f in futures)
    for entry in entries:
        if entry["ecosystem"] != "cargo" or "MPL" not in entry.get("license", ""):
            continue
        filename = f"{entry['name']}-{entry['version']}.crate"
        source = cache / filename
        target = output / "sources" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_file():
            raise FileNotFoundError(f"Cargo cache source absent: {filename}")
        if digest(source) != entry["checksum"]:
            raise ValueError(f"Cargo source checksum mismatch: {filename}")
        shutil.copyfile(source, target)
        sources.append({"name": entry["name"], "version": entry["version"], "file": target.relative_to(output).as_posix(),
                        "url": f"https://crates.io/api/v1/crates/{entry['name']}/{entry['version']}/download",
                        "sha256": digest(target)})
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for item in NATIVE_SOURCE_ARCHIVES:
            filename = f"{item['name']}-{item['version']}.tar.gz"
            futures.append((item, pool.submit(fetch, item["url"], output / "sources" / "native" / filename)))
        for item, future in futures:
            path = future.result()
            sources.append({**item, "file": path.relative_to(output).as_posix(), "sha256": digest(path)})
        for filename, url in NATIVE_RECIPE_FILES.items():
            path = fetch(url, output / "sources" / "native" / "lxml-winlibs-2026.10.05" / filename)
            sources.append({"name": filename, "version": "lxml/libxml2-win-binaries 2026.10.05",
                            "file": path.relative_to(output).as_posix(), "url": url, "sha256": digest(path)})
    return sources


def lxml_native_components(output: Path):
    """Record the native libraries bundled into the Windows lxml wheel."""
    try:
        dist = next(d for d in metadata.distributions() if canonical(d.metadata.get("Name", "")) == "lxml")
    except StopIteration:
        return []
    files = [Path(dist.locate_file(f)) for f in dist.files or []
             if Path(str(f)).name.lower() in {"licenses.txt", "license.txt"}]
    license_file = next((f for f in files if f.is_file() and f.name.lower() == "licenses.txt"), None)
    notice_list = []
    if license_file:
        target = output / "native" / "lxml-wheel" / "lxml-LICENSES.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_file, target)
        notice_list.append(target.relative_to(output).as_posix())
    # Use SPDX's canonical text endpoint: the GNU HTML site occasionally
    # throttles release builders, while this URL is immutable under the SPDX
    # license-list tag used by the archive.
    lgpl = fetch("https://raw.githubusercontent.com/spdx/license-list-data/main/text/LGPL-2.1-only.txt",
                 output / "native" / "libiconv-1.19" / "LGPL-2.1.txt",
                 "5749785c8bdefafcb5d798270ed0a967036fe2ca63dcedade1627565dfef81d2")
    provenance = output / "native" / "LXML_WINDOWS_NATIVE_PROVENANCE.md"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(
        f"""# lxml Windows native provenance

## What the release contains

The Windows runtime lock contains lxml 6.1.3. Its `lxml.etree` extension reports libxml2 2.11.9, libxslt 1.1.45 and the `iconv` feature. The locked wheel's extensions are copied unchanged into the frozen backend: the seven `.pyd` files in the table below were measured in the frozen directory and match the packaging virtual environment byte-for-byte. This fingerprints the shipped extension files; it does not establish that this project rebuilt them from source.

## Upstream provenance

The selected upstream recipe is `lxml/libxml2-win-binaries`, tag `{LXML_WINDOWS_TAG}` (annotated tag object `{LXML_WINDOWS_TAG_OBJECT}`, peeled commit `{LXML_WINDOWS_PEELED_COMMIT}`). It publishes the matching Windows assets below. The source archives are also included at the paths and commits recorded in `inventory.json`.

| win64 asset | SHA-256 |
|---|---|
| iconv-1.19.win64.zip | `4a92826a127789941ce920eeb6cc4289e40e241bf2bcab3067b00246dce608b5` |
| libxml2-2.11.9.win64.zip | `86dacde1187a9bdf46d12649ba1f7bcd00a041dd60e9461b033ea8546ee4d932` |
| libxslt-1.1.45.win64.zip | `32764ee260d1b5991afd2d1c49b133de31cebbc49c839e49316f029795043534` |
| zlib-1.3.2.win64.zip | `f78a02794a598afbe09ad7523be69dbd019665626562af798bf0ce971262ffb3` |

Release: https://github.com/lxml/libxml2-win-binaries/releases/tag/{LXML_WINDOWS_TAG}

The four bundled winlibs source archives correspond to these commits:

| component | source commit |
|---|---|
| libiconv | `accac417318a3eef7402685a268cab8037e62ba1` |
| libxml2 | `bb84678855e3407fc88180295c992f9c984e28e0` |
| libxslt | `c0076eaa6e0d1f99e01107e8db225a308eb1e543` |
| zlib | `0089522e62e6b5d4950e62bd20f0cb06a9288413` |

## Frozen lxml extension fingerprints

| extension | SHA-256 |
|---|---|
| `_elementpath.cp314-win_amd64.pyd` | `222be8036ba2f3bea485a4f68b8f8f484ccd7197d96fd60518ab778854c3151a` |
| `builder.cp314-win_amd64.pyd` | `5af41a278c7a9294bf34ddac3840b3ed40ed4f7e201d9a8c8e8c4a5e504ee1a4` |
| `etree.cp314-win_amd64.pyd` | `191eee78b34979c31ba08d952ca3cafabafe2f86d81adef19f0bcd4ad2ea53c9` |
| `objectify.cp314-win_amd64.pyd` | `6e7e0479add68e3c3764efcf72bd0d0660b13868c07755f72515246340687961` |
| `sax.cp314-win_amd64.pyd` | `9c48b8acb2c073ff3b79dfdbd80b0f1b60da99579f15fd61b57f8ebc09d74280` |
| `html/_difflib.cp314-win_amd64.pyd` | `5f5a5731a9ae72b156153dac0421ccb8244df0a28727ebe3b3b6bdd83467ce5c` |
| `html/diff.cp314-win_amd64.pyd` | `33d8838f47cb45b6d32b671851c724c71311d171a6ed4cd05de9b97a43b504d3` |

## Relinking guidance and verification boundary

1. Unpack the lxml 6.1.3 source distribution and the four winlibs source archives from `inventory.json`; retain their license files and notices.
2. Follow the pinned upstream Windows build recipe and its `build.ps1`, `.gitmodules`, `libiconv.patch` and CI workflow included under `sources/native/lxml-winlibs-2026.10.05/`. Build the native libraries from the included source commits, including libxml2 with iconv and zlib support.
3. Build lxml 6.1.3 against those source-built libraries, produce a replacement wheel, and replace the seven corresponding extensions in the PyInstaller environment before rebuilding the backend and installer.
4. Verify the replacement with the import/version checks in the lxml source tree, compare the resulting extension hashes, and exercise EPUB import/export and installer startup. Keep the modified application relinkable by distributing the replacement extension or a documented rebuild path with the corresponding object files/source as required by the applicable license terms.

For this preview, upstream release metadata, source archive commits and hashes, and frozen extension hashes have been recorded. **The native libraries and lxml extensions were not locally rebuilt from these source archives, and the replacement/relink procedure has not been rehearsed against the installer.** Do not interpret the provenance records as a successful local LGPL relink test or a legal opinion. This verification limitation remains open for a later release.
""",
        encoding="utf-8",
    )
    provenance_notice = provenance.relative_to(output).as_posix()
    return [
        {"ecosystem": "native", "name": "libiconv", "version": "1.19 (official lxml Windows binary tag 2026.10.05)",
         "license": "LGPL-2.1-or-later", "scope": "statically linked by bundled lxml etree extension; version provenance inferred from matching official build release",
         "notices": [lgpl.relative_to(output).as_posix(), *notice_list, provenance_notice],
         "upstream": "https://github.com/lxml/libxml2-win-binaries/releases/tag/2026.10.05",
         "source_archive": "sources/native/libiconv-winlibs-accac417318a3eef7402685a268cab8037e62ba1.tar.gz",
         "verification_status": "upstream provenance and frozen extension fingerprints recorded; local source rebuild and installer relink rehearsal not performed",
         "verification": "The installed wheel reports libxml2 2.11.9, libxslt 1.1.45 and the iconv feature. The pinned upstream release, matching asset hashes, source commits, and seven frozen lxml extension hashes are recorded in native/LXML_WINDOWS_NATIVE_PROVENANCE.md. The extensions match the locked build environment; they were not locally rebuilt from the supplied sources."},
        {"ecosystem": "native", "name": "libxml2", "version": "2.11.9", "license": "MIT",
         "scope": "bundled by lxml Windows wheel", "notices": notice_list,
         "source_archive": "sources/native/libxml2-winlibs-bb84678855e3407fc88180295c992f9c984e28e0.tar.gz"},
        {"ecosystem": "native", "name": "libxslt", "version": "1.1.45", "license": "MIT",
         "scope": "bundled by lxml Windows wheel", "notices": notice_list,
         "source_archive": "sources/native/libxslt-winlibs-c0076eaa6e0d1f99e01107e8db225a308eb1e543.tar.gz"},
        {"ecosystem": "native", "name": "zlib", "version": "1.3.2 (official lxml Windows binary tag 2026.10.05)", "license": "Zlib",
         "scope": "bundled by lxml Windows wheel", "notices": notice_list,
         "source_archive": "sources/native/zlib-winlibs-0089522e62e6b5d4950e62bd20f0cb06a9288413.tar.gz"},
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "THIRD_PARTY_NOTICES")
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--cargo-registry", type=Path, required=True)
    parser.add_argument("--rust-home", type=Path)
    parser.add_argument("--mingw", type=Path)
    parser.add_argument("--sources", action="store_true", help="Include exact copyleft source archives")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("Keep release output inside the project")
    if output.exists():
        if output.name not in {"THIRD_PARTY_NOTICES", "THIRD_PARTY_NOTICES_NEXT"}:
            raise ValueError("Refusing to replace a non-notices output directory")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    entries = python_components(output) + npm_components(output)
    entries += cargo_components(output, args.cargo, args.cargo_registry)
    entries += runtime_notices(output, args.rust_home, args.mingw)
    entries += lxml_native_components(output)
    sources = collect_sources(output, entries, args.cargo_registry.parent.parent / "cache" / args.cargo_registry.name) if args.sources else []
    errors = [{"name": e["name"], "ecosystem": e["ecosystem"], "error": e["error"]} for e in entries if e.get("error")]
    inventory = {"format": 1, "scope": "Python Windows runtime markers plus bootloader; npm production lock; Windows Cargo normal/build tree",
                 "components": entries, "sources": sources, "errors": errors,
                 "limits": ["A package inventory is not a legal opinion or proof of binary contents.",
                            "The lxml Windows extensions were not locally rebuilt from the supplied LGPL source or relink-tested in the installer; see native/LXML_WINDOWS_NATIVE_PROVENANCE.md.",
                            "Optional externally downloaded models, fonts and WebView2 retain separate terms."]}
    (output / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", "utf-8")
    hashes = [f"{digest(p)}  {p.relative_to(output).as_posix()}" for p in sorted(output.rglob("*"))
              if p.is_file() and p.name != "SHA256SUMS.txt"]
    (output / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", "utf-8")
    print(json.dumps({"components": len(entries), "sources": len(sources), "errors": errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
