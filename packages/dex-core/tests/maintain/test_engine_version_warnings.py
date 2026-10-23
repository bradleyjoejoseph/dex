"""Two engines sharing one `.dex/` are named on the envelope (#731).

The pin warning rides every envelope from `cli.main`, so it is driven here through
an explore command, a maintain command and a config verb. The state warnings ride
the commands that read a stored document: `_baseline_warnings` for every drift
axis, and the explore commands that fold a prior cache into the one they write.

Lives under `tests/maintain/` for the reason `test_unreadable_cache.py` gives:
`maintain_repo` is the fixture that builds a warehouse and a mapped cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import engine_version

RUNNING = "1.15.2"

LOCK = """\
version = 1

[[package]]
name = "exmergo-dex-core"
version = "{version}"
source = {{ registry = "https://pypi.org/simple" }}
"""


@pytest.fixture(autouse=True)
def _running(monkeypatch):
    """A released engine, rather than whatever dev version this checkout builds."""

    monkeypatch.setattr(engine_version, "running_version", lambda: RUNNING)


def _pin(root: Path, version: str) -> None:
    (root / "uv.lock").write_text(LOCK.format(version=version), encoding="utf-8")


def _version_warnings(payload: dict, needle: str) -> list[str]:
    return [w for w in payload["warnings"] if needle in w]


def _restamp(path: Path, *keys: str, value: str) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    target = doc
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize(
    "argv",
    [
        ("explore", "inventory"),
        ("maintain", "check"),
        ("config", "get"),
    ],
    ids=lambda a: " ".join(a),
)
def test_a_mismatched_pin_is_one_warning_naming_both_versions(maintain_repo, argv):
    maintain_repo.snapshot()
    _pin(maintain_repo.root, "1.14.0")

    _, payload = maintain_repo.dex(*argv)

    pinned = _version_warnings(payload, "this project pins")
    assert len(pinned) == 1, payload["warnings"]
    assert "1.14.0" in pinned[0] and RUNNING in pinned[0] and "uv.lock" in pinned[0]


def test_a_matching_pin_says_nothing(maintain_repo):
    _pin(maintain_repo.root, RUNNING)

    _, payload = maintain_repo.dex("explore", "inventory")

    assert _version_warnings(payload, "this project pins") == []


def test_the_cache_records_the_running_engine(maintain_repo):
    cache = json.loads((maintain_repo.root / ".dex" / "cache.json").read_text())

    assert cache["provenance"]["tool_version"] == RUNNING


def test_a_cache_from_another_release_warns_once_and_is_restamped(maintain_repo):
    cache_path = maintain_repo.root / ".dex" / "cache.json"
    _restamp(cache_path, "provenance", "tool_version", value="1.14.0")

    _, first = maintain_repo.dex("explore", "map")
    _, second = maintain_repo.dex("explore", "map")

    warned = _version_warnings(first, "the exploration cache was written by dex")
    assert len(warned) == 1 and "1.14.0" in warned[0] and RUNNING in warned[0]
    assert json.loads(cache_path.read_text())["provenance"]["tool_version"] == RUNNING
    assert _version_warnings(second, "the exploration cache was written by") == []


@pytest.mark.parametrize("recorded", [None, "0.0.0"])
def test_an_unrecorded_cache_stamp_is_not_a_mismatch(maintain_repo, recorded):
    cache_path = maintain_repo.root / ".dex" / "cache.json"
    doc = json.loads(cache_path.read_text())
    doc["provenance"]["tool_version"] = recorded
    cache_path.write_text(json.dumps(doc))

    _, payload = maintain_repo.dex("explore", "profile", "customers")

    assert _version_warnings(payload, "the exploration cache was written by") == []


def test_a_baseline_from_another_release_warns_until_it_is_retaken(maintain_repo):
    maintain_repo.snapshot()
    snapshot_path = maintain_repo.root / ".dex" / "snapshot.json"
    assert json.loads(snapshot_path.read_text())["tool_version"] == RUNNING
    _restamp(snapshot_path, "tool_version", value="1.14.0")

    _, stale = maintain_repo.dex("maintain", "schema")
    maintain_repo.snapshot()
    _, fresh = maintain_repo.dex("maintain", "schema")

    warned = _version_warnings(stale, "the drift baseline was written by dex")
    assert len(warned) == 1
    assert "1.14.0" in warned[0] and "maintain snapshot" in warned[0]
    assert _version_warnings(fresh, "the drift baseline was written by") == []
