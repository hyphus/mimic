from __future__ import annotations

from mimic.modules.discovery import specs_by_module_id
from mimic.modules.registry import list_modules


def test_discovers_shipped_modules():
    assert set(specs_by_module_id()) == {"docker_registry", "httpbin", "static", "webhook"}


def test_underscore_prefixed_packages_are_skipped():
    assert "_template" not in specs_by_module_id()


def test_list_modules_titles_come_from_the_manifest():
    modules = list_modules()
    assert set(modules) == {"docker_registry", "httpbin", "static", "webhook"}
    assert modules["httpbin"]["title"] == "HTTP Bin"
    assert modules["static"]["title"] == "Static Files"
    assert modules["webhook"]["host"] is None
