# Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
"""Launch the local UI with a private capability delivered in the URL fragment.

Examples: python scripts/run_local_secure.py; add --dev for the Vite frontend.
No public token bootstrap endpoint is exposed. Downloads/setup stay in the project.
"""
import argparse
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--private-link-file", type=Path, help="Save the local session link for the owning launcher to reopen; do not share this file")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    # Do not hand a token to a pre-existing/unrelated service on the chosen port.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            parser.error(f"backend port {args.port} is already occupied")
    token = secrets.token_urlsafe(32)
    environment = dict(os.environ, PLOTPILOT_SESSION_TOKEN=token)
    processes = []
    try:
        backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "interfaces.main:app",
                                    "--host", "127.0.0.1", "--port", str(args.port)],
                                   cwd=project, env=environment)
        processes.append(backend)
        ui_origin = f"http://127.0.0.1:{args.port}"
        if args.dev:
            npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
            if not npm:
                parser.error("npm is required for --dev")
            # Vite's existing proxy uses 8005; avoid a silent split backend.
            if args.port != 8005:
                parser.error("--dev currently uses backend port 8005")
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", 3000))
                except OSError:
                    parser.error("frontend port 3000 is already occupied")
            processes.append(subprocess.Popen([npm, "run", "dev", "--", "--strictPort"],
                                              cwd=project / "frontend", env=environment))
            ui_origin = "http://127.0.0.1:3000"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("A service exited before the UI was ready")
            try:
                with urllib.request.urlopen(ui_origin + "/", timeout=2) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError("The UI did not become ready in 120 seconds")
        launch_url = ui_origin + "/#plotpilot-session=" + token
        if args.private_link_file:
            args.private_link_file.parent.mkdir(parents=True, exist_ok=True)
            args.private_link_file.write_text(launch_url, encoding="utf-8")
        if args.no_browser:
            if args.private_link_file:
                print("Local UI ready. Private session saved for the owning launcher.", flush=True)
            else:
                print("Private launch link (do not share): " + launch_url, flush=True)
        else:
            webbrowser.open(launch_url)
            print("TaleWeft opened with a private local session. Press Ctrl+C to stop.", flush=True)
        backend.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


if __name__ == "__main__":
    main()
