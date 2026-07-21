# Contributing to Flock Finder

Thanks for helping improve Flock Finder! This guide covers local setup,
project conventions, and how to propose changes.

## TL;DR

```bash
git clone https://github.com/simeononsecurity/flock-finder.git
cd flock-finder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Run the checks CI runs:
ruff check scripts tests      # lint
pytest -q                     # unit tests
python3 scripts/oui_metadata.py     # regenerate data/flock_ouis.json
python3 scripts/validate_data.py    # validate published data
```

Open a pull request once `ruff`, `pytest`, and `validate_data.py` all pass.

## Project layout

| Path | Purpose |
|------|---------|
| `scripts/wigle_query.py` | Collector: queries WiGLE, merges + writes data |
| `scripts/validation.py` | Pure, unit-tested validation + data-policy helpers |
| `scripts/oui_metadata.py` | Loads canonical OUI CSV → generates `flock_ouis.json` |
| `scripts/validate_data.py` | CI data-integrity / precision checks |
| `data/flock_ouis.csv` | **Single source of truth** for OUI prefixes |
| `docs/index.html` | Interactive Leaflet map |
| `docs/DATA_POLICY.md` | What the data means + corrections process |
| `docs/DATA_DICTIONARY.md` | Field-level schema reference |
| `tests/` | Pytest suite |

## Adding or changing an OUI prefix

1. Edit **only** `data/flock_ouis.csv` (the canonical source). Use lowercase
   `xx:xx:xx` and fill in `source` / `notes`.
2. Regenerate the JSON mirror: `python3 scripts/oui_metadata.py`.
3. Run `python3 scripts/validate_data.py` to confirm it's well-formed and in
   sync. Do **not** hand-edit `data/flock_ouis.json` or the OUI list in
   `docs/index.html` — the frontend loads the JSON at runtime and falls back to
   an inline copy only if the fetch fails.

## Data policy (please read)

All records are **suspected** (OUI match only) and coordinates are truncated to
~110 m. Any change that could publish more precise coordinates, drop the
`match_confidence` label, or present matches as "confirmed" will be rejected.
See [docs/DATA_POLICY.md](docs/DATA_POLICY.md).

## Coding conventions

- **Python**: keep `scripts/validation.py` free of I/O so it stays testable.
  New parsing/validation logic belongs there with matching tests.
- **Frontend**: escape every WiGLE-sourced value with `escapeHtml(...)` before
  inserting it into the DOM. External links use `rel="noopener noreferrer"`.
- **Atomic writes**: data files are written via `atomic_write_json` /
  `atomic_write_text` so an interrupted run never corrupts output.

## Requesting data corrections

If you're a user (not a code contributor) and a point looks wrong, follow the
process in [docs/DATA_POLICY.md](docs/DATA_POLICY.md#5-requesting-a-correction-or-removal).

## Reporting security issues

Please avoid filing public issues for sensitive security problems. Use the
`/reportbug` process or contact the maintainer directly.
