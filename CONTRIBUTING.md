# Contributing

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e '.[dev]'
```

`requirements.txt` is the pinned lockfile the Docker image installs. `pyproject.toml` declares what
Mimic actually imports, plus the `dev` extra (pytest and ruff). When you add a direct dependency,
add it to both.

## Tests

```bash
pytest
```

The suite is a behavioural safety net, not a coverage target — it exists to catch regressions in
routing, settings precedence, auth, module loading, isolation, concurrency caps, and the SSE
stream. Tests run against a `tmp_path` data directory with `MIMIC_*` cleared from the environment
(see `tests/conftest.py`), so they never touch your local `data/`.

Deliberately broken module packages used by the isolation tests live in `tests/broken_modules/`;
they cannot live under `mimic/modules/` because discovery would load them in production.

## Lint

```bash
ruff check .
```

Configuration is in `pyproject.toml`. There is no formatter configured — match the surrounding
style.

## Running locally

```bash
export MIMIC_ADMIN_PASSWORD='choose-a-secret'
PYTHONPATH=. python -m mimic.dev
```

See [docs/modules.md](docs/modules.md) for how to add a module.
