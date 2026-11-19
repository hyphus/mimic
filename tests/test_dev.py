"""The dev server serves one socket, so it must not put /admin on a public interface."""

from __future__ import annotations

import pytest
from flask import Flask

from mimic import dev


@pytest.fixture
def runs(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: calls.append(kwargs))
    return calls


@pytest.mark.parametrize("public", ["0.0.0.0:8080", "10.0.0.5:8080", "[::]:8080", "example.com:8080", "8080"])
def test_refuses_a_public_bind_when_an_admin_bind_is_set(monkeypatch, runs, public):
    monkeypatch.setenv("MIMIC_PUBLIC_BIND", public)
    monkeypatch.setenv("MIMIC_ADMIN_BIND", "127.0.0.1:8443")
    with pytest.raises(SystemExit, match="/admin would be exposed"):
        dev.main()
    assert runs == []


@pytest.mark.parametrize("public", ["127.0.0.1:8080", "127.0.0.2:8080", "[::1]:8080", "localhost:8080"])
def test_runs_on_loopback_when_an_admin_bind_is_set(monkeypatch, runs, capsys, public):
    monkeypatch.setenv("MIMIC_PUBLIC_BIND", public)
    monkeypatch.setenv("MIMIC_ADMIN_BIND", "127.0.0.1:8443")
    dev.main()
    assert len(runs) == 1
    assert "ignoring it" in capsys.readouterr().err


def test_runs_on_any_interface_without_an_admin_bind(monkeypatch, runs):
    monkeypatch.setenv("MIMIC_PUBLIC_BIND", "0.0.0.0:8080")
    dev.main()
    assert runs[0]["host"] == "0.0.0.0"
