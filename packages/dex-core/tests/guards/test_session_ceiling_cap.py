"""A host-set cap on the daily ceiling (#787).

``budget.session_ceiling`` lives in a file the agent can write, and every route
that widens it is open to whoever drives the command line: the one-time ask's
two answers, ``dex config set|unset``, and the engine's own arguments. The cap is
the bound a host keeps out of that reach. These pin the property that makes it
one: with a cap set, the ceiling in force stays at or under it whatever route
was taken, while a process with no cap behaves exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from exmergo_dex_core import command_args
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import (
    Budget,
    DexConfig,
    DuckDBTarget,
    load_config,
    save_config,
)
from exmergo_dex_core.config_commands import config_set, config_unset
from exmergo_dex_core.config_document import ConfigDocument
from exmergo_dex_core.connect import new_cost_gate
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.errors import ConfigurationError, RequestError
from exmergo_dex_core.guards.cost_guard import CostGate, OverCeilingError
from exmergo_dex_core.results import to_envelope
from exmergo_dex_core.session_cap import (
    SESSION_CEILING_CAP_ENV,
    SessionCeilingCap,
    parse_session_ceiling_cap,
)
from exmergo_dex_core.storage import MemoryStore

CAP = SessionCeilingCap(value=100.0, source="environment")
MOOT = "does not change the daily ceiling in force"
HUGE = 1_000_000_000_000.0


def _committed(root: Path, **budget) -> Path:
    save_config(DexConfig(connector="bigquery", budget=Budget(**budget)), root)
    return root


def _gate_for(config: DexConfig, *, budget: float | None = None) -> CostGate:
    """The gate a billed command would get from this config, built where every
    billed command's gate is built."""

    return new_cost_gate(
        "bigquery", config, MemoryStore(), budget=budget, confirmed=True
    )


def _capped(root: Path, cap: SessionCeilingCap = CAP) -> DexConfig:
    return load_config(root).with_session_ceiling_cap(cap)


# --- parsing the cap ---------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_unset_cap_is_none(raw):
    assert parse_session_ceiling_cap(raw, source="environment") is None


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "nan", "inf", True])
def test_a_cap_that_is_not_a_positive_number_refuses(raw):
    with pytest.raises(ConfigurationError, match="positive number"):
        parse_session_ceiling_cap(raw, source="environment")


def test_a_cap_parses_from_text_in_the_connectors_unit():
    cap = parse_session_ceiling_cap(" 50000000000 ", source="environment")
    assert cap == SessionCeilingCap(value=50_000_000_000.0, source="environment")
    assert "DEX_SESSION_CEILING_CAP=50000000000" in cap.named()


# --- the minimum binds, whatever the file says -------------------------------------


def test_the_cap_binds_over_a_higher_committed_ceiling(tmp_path):
    config = _capped(_committed(tmp_path, ceiling=HUGE, session_ceiling=HUGE))
    assert config.session_ceiling_in_force == CAP.value
    assert config.session_ceiling_source == "environment"
    assert _gate_for(config).session_ceiling == CAP.value


def test_a_lower_committed_ceiling_still_binds_and_is_named_as_config(tmp_path):
    config = _capped(_committed(tmp_path, ceiling=HUGE, session_ceiling=10.0))
    assert config.session_ceiling_in_force == 10.0
    assert config.session_ceiling_source == "config"


def test_the_cap_alone_binds_where_the_file_sets_no_ceiling(tmp_path):
    config = _capped(_committed(tmp_path, ceiling=HUGE))
    gate = _gate_for(config)
    assert gate.session_ceiling == CAP.value
    # The cap is the decision, so a capped project is never asked for one.
    assert not gate.session_ceiling_pending(50.0)
    assert gate.preflight_command(50.0).estimate == 50.0
    assert gate.warnings() == []


def test_the_cap_binds_over_a_recorded_decline(tmp_path):
    config = _capped(_committed(tmp_path, ceiling=HUGE, session_ceiling_declined=True))
    assert config.session_ceiling_in_force == CAP.value
    assert config.session_ceiling_declined_in_force is False
    gate = _gate_for(config)
    assert gate.session_ceiling == CAP.value
    # Bounded, so nothing says the day is unbounded.
    gate.preflight_command(50.0)
    assert gate.warnings() == []


def test_the_cap_never_dumps_into_the_file(tmp_path):
    config = _capped(_committed(tmp_path, ceiling=HUGE))
    assert "session_ceiling_cap" not in json.dumps(config.model_dump(mode="json"))


def test_the_cap_is_applied_to_a_copy_not_to_the_hosts_object():
    host_config = DexConfig(connector="bigquery")
    engine = DexEngine(config=host_config, session_ceiling_cap=5.0)
    assert engine.config.session_ceiling_in_force == 5.0
    assert host_config.session_ceiling_cap is None


# --- every widening route from the issue, each held under the cap ------------------


def test_a_session_ceiling_answer_above_the_cap_is_written_and_moot(tmp_path):
    _committed(tmp_path, ceiling=HUGE)
    engine = DexEngine.from_repo(
        tmp_path, session_ceiling=HUGE, session_ceiling_cap=CAP
    )
    # Written, as asked: the decision is the project's and still reviewable.
    assert load_config(tmp_path).budget.session_ceiling == HUGE
    assert engine.config_diffs
    # Moot while the cap holds, and said so.
    assert engine.config.session_ceiling_in_force == CAP.value
    assert _gate_for(engine.config).session_ceiling == CAP.value
    (warning,) = engine.budget_warnings
    assert "--session-ceiling 1000000000000" in warning
    assert "DEX_SESSION_CEILING_CAP=100" in warning


def test_a_decline_answer_cannot_unbound_a_capped_day(tmp_path):
    _committed(tmp_path, ceiling=HUGE)
    engine = DexEngine.from_repo(
        tmp_path, decline_session_ceiling=True, session_ceiling_cap=CAP
    )
    assert load_config(tmp_path).budget.session_ceiling_declined is True
    assert engine.config.session_ceiling_in_force == CAP.value
    assert engine.config.session_ceiling_declined_in_force is False
    assert engine.budget_warnings


def test_constructor_answers_from_a_library_host_are_held_too(tmp_path):
    _committed(tmp_path, ceiling=HUGE)
    engine = DexEngine.from_repo(
        tmp_path, session_ceiling=HUGE, session_ceiling_cap=7.0
    )
    assert engine.session_ceiling_cap == SessionCeilingCap(7.0, "host")
    assert _gate_for(engine.config).session_ceiling == 7.0
    assert "DexEngine(session_ceiling_cap=7)" in engine.budget_warnings[0]


def test_an_answer_that_lowers_the_ceiling_in_force_is_not_warned(tmp_path):
    _committed(tmp_path, ceiling=HUGE)
    engine = DexEngine.from_repo(
        tmp_path, session_ceiling=10.0, session_ceiling_cap=CAP
    )
    assert engine.config.session_ceiling_in_force == 10.0
    assert engine.budget_warnings == []


@pytest.mark.parametrize(
    ("budget", "pairs"),
    [
        ({"session_ceiling": 50.0}, ["budget.session_ceiling", "1000000000000"]),
        ({}, ["budget.session_ceiling_declined", "true"]),
        ({"session_ceiling": 500.0}, ["budget.session_ceiling", "900"]),
    ],
)
def test_config_set_cannot_lift_the_ceiling_above_the_cap(tmp_path, budget, pairs):
    _committed(tmp_path, ceiling=HUGE, **budget)
    result = config_set(tmp_path, pairs, confirmed=True, session_cap=CAP)
    assert result.written
    assert _capped(tmp_path).session_ceiling_in_force <= CAP.value


def test_config_set_that_changes_nothing_in_force_says_so(tmp_path):
    _committed(tmp_path, ceiling=HUGE, session_ceiling=500.0)
    result = config_set(
        tmp_path, ["budget.session_ceiling", "900"], confirmed=False, session_cap=CAP
    )
    assert any(
        "does not change the daily ceiling in force" in w for w in result.warnings
    )
    assert any("DEX_SESSION_CEILING_CAP=100" in w for w in result.warnings)


def test_config_set_without_a_cap_carries_no_such_warning(tmp_path):
    _committed(tmp_path, ceiling=HUGE, session_ceiling=500.0)
    result = config_set(tmp_path, ["budget.session_ceiling", "900"], confirmed=False)
    assert not any("ceiling in force" in w for w in result.warnings)


def test_config_unset_cannot_unbound_a_capped_day(tmp_path):
    _committed(tmp_path, ceiling=HUGE, session_ceiling=500.0)
    result = config_unset(
        tmp_path, ["budget.session_ceiling"], confirmed=True, session_cap=CAP
    )
    assert result.written
    assert load_config(tmp_path).budget.session_ceiling is None
    assert _capped(tmp_path).session_ceiling_in_force == CAP.value
    assert any(
        "does not change the daily ceiling in force" in w for w in result.warnings
    )


def test_a_budget_above_the_cap_only_meets_the_capped_remainder():
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=HUGE,  # a --budget far above the cap
        session_ceiling=CAP.value,
        session_spent=40.0,
        confirmed=True,
        connector="bigquery",
        session_ceiling_cap=CAP,
    )
    with pytest.raises(OverCeilingError) as refused:
        gate.preflight_command(80.0)
    message = str(refused.value)
    assert "DEX_SESSION_CEILING_CAP=100" in message
    assert "only the host can" in message


def test_a_statement_charged_past_the_cap_names_it_too():
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=HUGE,
        session_ceiling=CAP.value,
        session_spent=0.0,
        confirmed=True,
        connector="bigquery",
        session_ceiling_cap=CAP,
    )
    gate.preflight_command(60.0)
    with pytest.raises(OverCeilingError, match="only the host can"):
        gate.charge(150.0)


# --- the refusal names whichever bound bound ---------------------------------------


def _refusal(**overrides) -> str:
    kwargs = {
        "paradigm": Paradigm.BYTES_SCANNED,
        "ceiling": 1_000.0,
        "session_ceiling": CAP.value,
        "session_spent": 0.0,
        "confirmed": True,
        "connector": "bigquery",
    }
    kwargs.update(overrides)
    with pytest.raises(OverCeilingError) as refused:
        CostGate(**kwargs).preflight_command(500.0)
    return str(refused.value)


def test_without_a_cap_the_refusal_reads_exactly_as_it_always_has():
    assert _refusal() == (
        "estimated cost 500.0 exceeds the ceiling 100.0 (bytes_scanned); raise "
        "the budget or narrow the work"
    )


def test_a_lower_committed_ceiling_is_not_blamed_on_the_cap():
    message = _refusal(
        session_ceiling=50.0,
        session_ceiling_cap=SessionCeilingCap(value=100.0, source="host"),
    )
    assert "host" not in message


def test_a_per_command_budget_tighter_than_the_cap_is_not_blamed_on_it():
    message = _refusal(ceiling=10.0, session_ceiling_cap=CAP)
    assert "host" not in message


def test_the_phase_advice_names_the_cap_rather_than_the_config_file():
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=HUGE,
        session_ceiling=CAP.value,
        session_spent=0.0,
        confirmed=True,
        connector="bigquery",
        session_ceiling_cap=CAP,
    )
    gate.preflight_command(60.0)
    request = command_args.phase_handshake(
        "explore relationships",
        SimpleNamespace(cost_gate=gate),
        150.0,
        phase="verify",
        hint=lambda *_: "re-run",
    )
    assert request is not None
    notes = " ".join(request.data["notes"])
    assert "only the host can" in notes
    assert "raise the session budget in .dex/config.yml" not in notes


# --- the file can never hold the cap ----------------------------------------------


@pytest.mark.parametrize("key", ["session_ceiling_cap", "budget.session_ceiling_cap"])
def test_config_set_refuses_to_write_the_cap(tmp_path, key):
    _committed(tmp_path, ceiling=HUGE)
    with pytest.raises(RequestError, match="set by the host"):
        config_set(tmp_path, [key, "5"], confirmed=True)


def test_config_validate_names_a_nested_cap_as_one_the_file_cannot_hold(tmp_path):
    _committed(tmp_path, ceiling=HUGE)
    path = tmp_path / ".dex" / "config.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "budget:\n", "budget:\n  session_ceiling_cap: 5\n"
        ),
        encoding="utf-8",
    )
    problems = ConfigDocument.read(path).problems()
    (problem,) = [p for p in problems if p.key == "budget.session_ceiling_cap"]
    assert problem.kind == "caller_only_key"
    assert problem.fatal is False
    # And a file that writes one still caps nothing.
    assert load_config(tmp_path).session_ceiling_cap is None


# --- the CLI and the library report it ---------------------------------------------


def run(argv: list[str], capsys) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def repo(duckdb_file: Path, tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / ".dex").mkdir(parents=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {duckdb_file}\n", encoding="utf-8"
    )
    monkeypatch.chdir(root)
    monkeypatch.delenv(SESSION_CEILING_CAP_ENV, raising=False)
    return root


def test_without_a_cap_nothing_is_stamped(repo, capsys):
    _code, ok = run(["explore", "inventory"], capsys)
    assert "session_ceiling_cap" not in ok["data"]


def test_the_environment_cap_stamps_every_envelope(repo, capsys, monkeypatch):
    monkeypatch.setenv(SESSION_CEILING_CAP_ENV, "100")

    code, ok = run(["explore", "inventory"], capsys)
    assert code == 0
    assert ok["data"]["session_ceiling_cap"] == {
        "value": 100.0,
        "unit": None,  # DuckDB counts nothing, so the cap is reported, not applied
        "source": "environment",
    }

    code, refused = run(["explore", "profile", "no_such_table"], capsys)
    assert code == 1
    assert refused["data"]["session_ceiling_cap"]["source"] == "environment"

    code, got = run(["config", "get", "budget"], capsys)
    assert code == 0
    assert got["data"]["session_ceiling_cap"]["value"] == 100.0


def test_a_malformed_environment_cap_refuses_every_command(repo, capsys, monkeypatch):
    monkeypatch.setenv(SESSION_CEILING_CAP_ENV, "-1")
    for argv in (["explore", "inventory"], ["config", "get"]):
        code, refused = run(argv, capsys)
        assert code == 1
        assert refused["reason"] == "configuration"
        assert SESSION_CEILING_CAP_ENV in refused["errors"][0]


def test_a_cli_answer_under_the_cap_is_written_with_a_warning(
    repo, capsys, monkeypatch
):
    monkeypatch.setenv(SESSION_CEILING_CAP_ENV, "100")
    code, ok = run(["explore", "inventory", "--session-ceiling", "5000"], capsys)
    assert code == 0
    assert ok["diffs"], "the answer is still recorded as a reviewable diff"
    assert any(
        "does not change the daily ceiling in force" in w for w in ok["warnings"]
    )
    assert load_config(repo).budget.session_ceiling == 5000.0


def test_config_set_under_the_cap_warns_through_the_cli(repo, capsys, monkeypatch):
    monkeypatch.setenv(SESSION_CEILING_CAP_ENV, "100")
    code, proposed = run(["config", "set", "budget.session_ceiling", "5000"], capsys)
    assert code == 0
    assert proposed["status"] == "needs_confirmation"
    assert any(
        "does not change the daily ceiling in force" in w for w in proposed["warnings"]
    )


def test_the_library_reports_the_hosts_cap_on_every_result(duckdb_file: Path):
    engine = DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=DexConfig(connector="duckdb"),
        session_ceiling_cap=250.0,
    )
    result = engine.connect_test()
    assert result.session_ceiling_cap == {
        "value": 250.0,
        "unit": None,
        "source": "host",
    }
    assert to_envelope(result).data["session_ceiling_cap"]["source"] == "host"


def test_an_uncapped_library_result_carries_no_cap(duckdb_file: Path):
    engine = DexEngine(
        connector="duckdb", path=str(duckdb_file), config=DexConfig(connector="duckdb")
    )
    result = engine.connect_test()
    assert result.session_ceiling_cap is None
    assert "session_ceiling_cap" not in to_envelope(result).data


def test_a_malformed_library_cap_refuses_at_construction():
    with pytest.raises(ConfigurationError, match="session_ceiling_cap"):
        DexEngine(connector="duckdb", session_ceiling_cap=0)


def test_a_library_engine_carries_config_diff_and_cap_warning_on_first_result(
    tmp_path: Path, duckdb_file: Path
):
    save_config(
        DexConfig(
            connector="duckdb",
            duckdb=DuckDBTarget(path=str(duckdb_file)),
            budget=Budget(ceiling=HUGE),
        ),
        tmp_path,
    )
    engine = DexEngine.from_repo(
        tmp_path, session_ceiling=HUGE, session_ceiling_cap=CAP
    )
    assert engine.config_diffs
    assert engine.budget_warnings

    first = engine.connect_test()
    assert first.diffs == engine.config_diffs
    assert any(MOOT in w for w in first.warnings)
    first_envelope = to_envelope(first)
    assert first_envelope.diffs == engine.config_diffs
    assert any(MOOT in w for w in first_envelope.warnings)

    second = engine.connect_test()
    assert second.diffs == []
    assert not any(MOOT in w for w in second.warnings)
    second_envelope = to_envelope(second)
    assert second_envelope.diffs == []
    assert not any(MOOT in w for w in second_envelope.warnings)

    assert engine.config_diffs
    assert engine.budget_warnings


def test_a_library_engine_carries_decline_diff_and_cap_warning_on_first_result(
    tmp_path: Path, duckdb_file: Path
):
    save_config(
        DexConfig(
            connector="duckdb",
            duckdb=DuckDBTarget(path=str(duckdb_file)),
            budget=Budget(ceiling=HUGE),
        ),
        tmp_path,
    )
    engine = DexEngine.from_repo(
        tmp_path, decline_session_ceiling=True, session_ceiling_cap=CAP
    )
    first = engine.connect_test()
    assert first.diffs == engine.config_diffs
    assert any(MOOT in w for w in first.warnings)

    second = engine.connect_test()
    assert second.diffs == []
    assert not any(MOOT in w for w in second.warnings)
