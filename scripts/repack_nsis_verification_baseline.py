# Modified for TaleWeft on 2026-10-22: independent branding; upstream attribution retained.
"""Repackage an extracted historical NSIS payload under a QA-only identity.

This is used only for the old baseline installer. It never runs the source
installer and cannot reuse the production registry key because the rendered
template is rewritten to a distinct product name and publisher.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess


_DEFINE = re.compile(r'(!define\s+)(MANUFACTURER|PRODUCTNAME|BUNDLEID|OUTFILE)\s+"[^"]*"')
_ONAME_FILE = re.compile(r'(\s*File\s+/a\s+"/oname=)([^"]+)("\s+")[^"]+(".*)$')
_MAIN_FILE = re.compile(r'(\s*File\s+")\$\{MAINBINARYSRCPATH\}(".*)$')
_WEBVIEW_FILE = re.compile(r'(\s*File\s+"/oname=\$TEMP\\MicrosoftEdgeWebview2Setup\.exe"\s+")\$\{WEBVIEW2BOOTSTRAPPERPATH\}(".*)$')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True,
                        help="rendered Tauri installer.nsi from the candidate build")
    parser.add_argument("--payload", type=Path, required=True,
                        help="7-Zip extracted baseline payload")
    parser.add_argument("--out-script", type=Path, required=True)
    parser.add_argument("--out-installer", type=Path, required=True)
    parser.add_argument("--product-name", required=True)
    parser.add_argument("--publisher", default="TaleWeft Verification")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--makensis", type=Path, required=True)
    args = parser.parse_args()

    if (args.product_name.casefold() in {"taleweft", "plotpilot"}
            or args.publisher.casefold() in {"taleweft", "plotpilot team"}):
        raise SystemExit("production identity is forbidden")
    payload = args.payload.resolve()
    if not (payload / "plotpilot.exe").is_file() or not (payload / "plotpilot-backend" / "plotpilot-backend.exe").is_file():
        raise SystemExit("payload is not an extracted PlotPilot installer")

    text = args.template.read_text(encoding="utf-8-sig")
    # The new candidate template names taleweft.exe; the old payload still
    # installs plotpilot.exe. Keep shortcuts, registry and uninstall consistent.
    text = re.sub(r'(?m)^!define MAINBINARYNAME "[^"]+"$',
                  '!define MAINBINARYNAME "plotpilot"', text)
    values = {
        "MANUFACTURER": args.publisher,
        "PRODUCTNAME": args.product_name,
        "BUNDLEID": args.bundle_id,
        "OUTFILE": str(args.out_installer.resolve()),
    }

    def define(match: re.Match[str]) -> str:
        return f'{match.group(1)}{match.group(2)} "{values[match.group(2)]}"'

    text = _DEFINE.sub(define, text)
    text = re.sub(r'(?m)^OutFile\s+"[^"]+"$', f'OutFile "{args.out_installer.resolve()}"', text)
    text = _MAIN_FILE.sub(lambda m: f'{m.group(1)}{(payload / "plotpilot.exe").as_posix()}{m.group(2)}', text)
    webview = payload / "$TEMP" / "MicrosoftEdgeWebview2Setup.exe"
    if webview.is_file():
        text = _WEBVIEW_FILE.sub(lambda m: f'{m.group(1)}{webview.as_posix()}{m.group(2)}', text)

    missing = []

    def payload_file(match: re.Match[str]) -> str:
        target = match.group(2).replace("\\", "/")
        source = payload / target
        if not source.is_file():
            missing.append(target)
        return f'{match.group(1)}{target}{match.group(3)}{source.as_posix()}{match.group(4)}'

    text = _ONAME_FILE.sub(payload_file, text)
    if missing:
        raise SystemExit(f"baseline payload is missing {len(missing)} files, first: {missing[0]}")
    args.out_script.parent.mkdir(parents=True, exist_ok=True)
    args.out_script.write_text(text, encoding="utf-8")
    args.out_installer.parent.mkdir(parents=True, exist_ok=True)
    # Tauri's cached makensis is a tiny launcher that resolves the bundled
    # toolset relative to its working directory; invoke it from that folder.
    makensis = args.makensis.resolve()
    # The cached Tauri makensis launcher is a PE shim which Windows Python's
    # CreateProcess cannot execute directly in this environment; cmd.exe can.
    command = f'"{makensis}" "{args.out_script.resolve()}"'
    subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command],
                   check=True, cwd=makensis.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
