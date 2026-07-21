"""Tests for scripts/oui_metadata.py and the canonical OUI dataset."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from oui_metadata import list_ouis, load_oui_metadata, write_oui_json  # noqa: E402
from validation import is_valid_oui  # noqa: E402


def test_load_oui_metadata_all_valid():
    entries = load_oui_metadata()
    assert entries, "expected at least one OUI entry"
    for e in entries:
        assert is_valid_oui(e["oui"]), f"invalid OUI slipped through: {e['oui']}"
        assert e["oui"] == e["oui"].upper()


def test_load_oui_metadata_no_duplicates():
    ouis = list_ouis()
    assert len(ouis) == len(set(ouis)), "duplicate OUI prefixes in dataset"


def test_write_oui_json_roundtrip(tmp_path):
    import json

    out = tmp_path / "flock_ouis.json"
    write_oui_json(json_path=out)
    payload = json.loads(out.read_text())
    assert payload["match_confidence"] == "suspected"
    assert payload["total"] == len(payload["ouis"])
    assert payload["total"] >= 1
