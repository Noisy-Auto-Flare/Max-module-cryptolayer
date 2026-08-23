# AGENTS.md — max-transport-module

## Purpose
Workspace for the `Max` transport module for CryptoLayer: messaging via the unofficial MAX user API (vkmax) with E2E encryption handled by the CryptoLayer core. Plan: `.omo/plans/max-transport-module.md` (do not modify).

## Layout
- `Max/` — module folder (`__init__.py` MUST stay EMPTY; `main.py`; `requirements.txt` comes in task 2)
- `tests/`, `scripts/`, `docs/` — tests, dev scripts, docs (ADRs in `docs/DECISIONS.md`)
- `ruff.toml`, `requirements-dev.txt` — dev tooling

## Environment
- Host: Windows; venv at `.venv` — run via `.venv\Scripts\python.exe`
- Python: 3.14.3 (>= 3.10 required)

## Interface pin
- cryptolayer-module-interface @ git+https://github.com/igmunv/cryptolayer-module-interface.git, commit `34cb4a4c079ae1326c3d65f89147f95898e1aa6c` (provides top-level module `base_module`: classes `BaseModule`, `Credential`)

## Commands
- Tests: `.venv\Scripts\python.exe -m pytest -q`
- Lint: `.venv\Scripts\python.exe -m ruff check .`

## Rules
- NEVER commit secrets/tokens/sessions (see `.gitignore`).
- Do not touch foreign cryptolayer* repos from here.
- Module folder convention (`__init__.py` empty, `main.py`) is fixed.
