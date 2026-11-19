"""Optional deferred SIGHUP to the Gunicorn master (parent process) after config changes."""

from __future__ import annotations

import os
import signal
import threading
import time

from mimic.settings import load_server_settings


def schedule_gunicorn_master_reload(*, delay_s: float = 0.35) -> bool:
    """
    If MIMIC_GUNICORN_RELOAD_HUP is truthy, send SIGHUP to getppid() after a short delay
    so the admin redirect response can finish. Returns True if a signal was scheduled.
    """
    if not load_server_settings().gunicorn_reload_hup:
        return False
    ppid = os.getppid()
    if ppid <= 1:
        return False

    def _run() -> None:
        time.sleep(delay_s)
        try:
            os.kill(ppid, signal.SIGHUP)
        except OSError:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return True
