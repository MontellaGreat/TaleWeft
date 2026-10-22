"""Bounded local API audit metadata; request bodies and credentials never enter it."""
import json
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SecurityAuditLog:
    def __init__(self, directory):
        self.path = Path(directory) / "security-audit.jsonl"
        self._lock = threading.Lock()
        self._handler = None
        self.last_error = None

    def record(self, *, method, route, status):
        event = {"time": datetime.now(timezone.utc).isoformat(), "method": method,
                 "route": route, "status": status,
                 "outcome": "rejected" if status in (401, 403) else "failed" if status >= 400 else "accepted"}
        with self._lock:
            try:
                if self._handler is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._handler = RotatingFileHandler(self.path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
                    self._handler.setFormatter(logging.Formatter("%(message)s"))
                text = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                # Invoke the file methods directly so filesystem failure is observable.
                record = logging.LogRecord("security_audit", logging.INFO, "", 0, text, (), None)
                if self._handler.shouldRollover(record):
                    self._handler.doRollover()
                self._handler.stream.write(text + "\n")
                self._handler.flush()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__

    def read(self, limit=100):
        with self._lock:
            events = []
            for path in [Path(str(self.path) + ".2"), Path(str(self.path) + ".1"), self.path]:
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
            return {"status": "unavailable" if self.last_error else "available",
                    "error": self.last_error, "events": list(reversed(events[-max(1, min(500, limit)):])),
                    "max_files": 3, "max_bytes_per_file": 1024 * 1024}

    def close(self):
        with self._lock:
            if self._handler:
                self._handler.close()
                self._handler = None
