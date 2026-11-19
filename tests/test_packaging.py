from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_gunicorn_config_at_repo_root():
    assert (REPO_ROOT / "gunicorn.conf.py").is_file()


def test_requirements_has_no_deprecated_dotenv_placeholder():
    reqs = (REPO_ROOT / "requirements.txt").read_text().splitlines()
    names = {line.split("==")[0].strip().lower() for line in reqs if line.strip()}
    assert "python-dotenv" in names
    assert "dotenv" not in names
