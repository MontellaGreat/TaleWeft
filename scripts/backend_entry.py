"""Loopback-only entry point for the open-source PyInstaller backend bundle."""
from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path
import sys


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument('port', type=int)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import uvicorn
    uvicorn.run('interfaces.main:app', host='127.0.0.1', port=args.port,
                log_level='info', proxy_headers=False)


if __name__ == '__main__':
    main()
