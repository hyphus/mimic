from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mimic.dispatch import GLOBAL_MODULE_CONCURRENCY, PrefixDispatcherMiddleware
from mimic.modules.schema import DEFAULT_MAX_CONCURRENT as DEFAULT_MODULE_CONCURRENCY

REPO_ROOT = Path(__file__).resolve().parents[1]


def _blocking_app(gate, entered):
    def app(environ, start_response):
        entered.release()
        gate.wait()
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    return app


def _status(mw, path):
    captured = []
    body = mw({"PATH_INFO": path}, lambda status, headers: captured.append((status, headers)))
    if hasattr(body, "close"):
        body.close()
    return captured[0]


def test_per_module_cap_sheds_load_and_recovers():
    gate = threading.Event()
    entered = threading.Semaphore(0)
    main_seen: list[str] = []

    def main(environ, start_response):
        main_seen.append(environ["PATH_INFO"])
        start_response("200 OK", [])
        return [b"main"]

    mw = PrefixDispatcherMiddleware(main, lambda: (("/mod", _blocking_app(gate, entered), DEFAULT_MODULE_CONCURRENCY),))

    with ThreadPoolExecutor(max_workers=DEFAULT_MODULE_CONCURRENCY) as pool:
        holders = [pool.submit(_status, mw, "/mod/slow") for _ in range(DEFAULT_MODULE_CONCURRENCY)]
        for _ in range(DEFAULT_MODULE_CONCURRENCY):
            assert entered.acquire(timeout=5)

        status, headers = _status(mw, "/mod/one-too-many")
        assert status.startswith("503")
        assert dict(headers)["Retry-After"]

        # Unmounted paths bypass the cap entirely.
        assert _status(mw, "/healthz")[0].startswith("200")
        assert main_seen == ["/healthz"]

        gate.set()
        for f in holders:
            assert f.result(timeout=5)[0].startswith("200")

    assert _status(mw, "/mod/after")[0].startswith("200")


def test_global_cap_applies_across_modules():
    gate = threading.Event()
    entered = threading.Semaphore(0)
    pairs = (
        ("/a", _blocking_app(gate, entered), DEFAULT_MODULE_CONCURRENCY),
        ("/b", _blocking_app(gate, entered), DEFAULT_MODULE_CONCURRENCY),
    )
    mw = PrefixDispatcherMiddleware(lambda e, s: [b""], lambda: pairs)

    per_module = min(DEFAULT_MODULE_CONCURRENCY, GLOBAL_MODULE_CONCURRENCY)
    spare = GLOBAL_MODULE_CONCURRENCY - per_module

    with ThreadPoolExecutor(max_workers=GLOBAL_MODULE_CONCURRENCY) as pool:
        holders = [pool.submit(_status, mw, "/a/slow") for _ in range(per_module)]
        holders += [pool.submit(_status, mw, "/b/slow") for _ in range(spare)]
        for _ in range(GLOBAL_MODULE_CONCURRENCY):
            assert entered.acquire(timeout=5)

        # /b is under its own cap but the global budget is spent.
        assert _status(mw, "/b/one-too-many")[0].startswith("503")

        gate.set()
        for f in holders:
            assert f.result(timeout=5)[0].startswith("200")


def test_global_cap_leaves_headroom_for_admin_traffic():
    threads = {}
    exec(compile((REPO_ROOT / "gunicorn.conf.py").read_text(), "gunicorn.conf.py", "exec"), threads)
    assert GLOBAL_MODULE_CONCURRENCY < threads["threads"]
