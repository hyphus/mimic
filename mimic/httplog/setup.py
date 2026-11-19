"""Root logger configuration and the in-memory buffer the dashboard tails.

This is the application's own log stream. The HTTP exchanges shown under
*Request log* are a different thing entirely and live in `mimic.httplog.store`.
"""

from __future__ import annotations

import logging
import threading

from datetime import datetime

from mimic.events import publish
from mimic.settings import DASHBOARD_BUFFER_SIZE, load_logging_settings

# Entries are {"id": int, "text": str}; the monotonic id lets a client that fell
# behind work out where to resume.
dashboard_log_buffer: list[dict] = []
dashboard_log_lock = threading.Lock()
_dashboard_log_seq = 0


class LogOutputHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _dashboard_log_seq

        try:
            ts = datetime.now(datetime.now().astimezone().tzinfo).strftime("%m-%d-%Y %H:%M:%S %Z")
            self.format(record)
            if record.module == "_internal":  # The _internal module is werkzeug's debug and request logging
                return
            entry = {"id": 0, "text": f"[{ts}] [{record.levelname}] {record.message}"}
            with dashboard_log_lock:
                _dashboard_log_seq += 1
                entry["id"] = _dashboard_log_seq
                if len(dashboard_log_buffer) >= DASHBOARD_BUFFER_SIZE:  # Keep a rolling buffer
                    dashboard_log_buffer.pop(0)
                dashboard_log_buffer.append(entry)
            publish("applog", entry)
        except Exception:
            self.handleError(record)


# Filter out the log polling endpoint from werkzeug logs to reduce noise in the console.
class LogPollingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/admin/logs/poll" not in record.getMessage()


def setup_logging() -> logging.Logger:
    """Configure the root logger to capture all logs to the dashboard buffer."""
    root_logger = logging.getLogger()

    level_names = logging.getLevelNamesMapping()
    log_level = level_names.get(load_logging_settings().level, logging.INFO)  # Defaults to INFO if invalid
    root_logger.setLevel(log_level)

    # Add the custom handler to the root logger so all child loggers inherit it
    handler = LogOutputHandler()
    formatter = logging.Formatter(fmt="{asctime} [{levelname}] {message}", style="{", datefmt="%Y-%m-%d %H:%M:%S UTC")
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Filter out werkzeug's log polling endpoint to reduce noise
    logging.getLogger("werkzeug").addFilter(LogPollingFilter())

    return root_logger


def get_dashboard_log_entries() -> list[dict]:
    """Return a copy of all dashboard log entries."""
    with dashboard_log_lock:
        return list(dashboard_log_buffer)
