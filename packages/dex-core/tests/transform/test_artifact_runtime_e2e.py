"""Cross-runtime artifact isolation, exercised without two dbt installs (#862).

The supported pair is one shared ``target/`` and a second runtime key. Process
behavior is a fake ``dbt build`` argv recorder. Directory-vs-file collisions
are planted on disk, which is the failure incompatible dbt versions leave
behind.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.transform.test_build_evidence import (
    node,
    write_manifest_nodes,
    write_run_results,
)

from exmergo_dex_core.transform.artifact_runtime import (
    ForeignInvocationError,
    assert_same_invocation,
    prepare_artifact_dir,
)
from exmergo_dex_core.transform.build import build
from exmergo_dex_core.transform.evidence import BuildOutcome, build_evidence

_RUNTIME_B = "dbt-1.10.11"


def _plant_directory_as_sql(shared: Path) -> Path:
    """Snapshot compiled SQL left as a directory by the other runtime."""

    snap = shared / "run" / "demo" / "snapshots" / "check_row.sql"
    snap.mkdir(parents=True)
    (snap / "nested.sql").write_text("select 1\n", encoding="utf-8")
    return snap


def _write_success(directory: Path, invocation_id: str = "abc-123") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "invocation_id": invocation_id,
                    "dbt_version": "1.11.11",
                    "adapter_version": "1.10.11",
                    "generated_at": datetime.now(UTC).isoformat(),
                },
                "results": [node("mart")],
                "elapsed_time": 0.2,
            }
        ),
        encoding="utf-8",
    )
    compiled_at = datetime.now(UTC) + timedelta(minutes=1)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "dex_test",
                    "generated_at": compiled_at.isoformat(),
                },
                "nodes": {
                    "model.dex_test.mart": {
                        "name": "mart",
                        "resource_type": "model",
                        "relation_name": '"dev"."main"."mart"',
                        "config": {"materialized": "view"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_cross_runtime_handoff_reads_the_isolated_directory(dbt_project_dir: Path):
    shared = dbt_project_dir / "target"
    planted = _plant_directory_as_sql(shared)
    isolated = prepare_artifact_dir(dbt_project_dir, _RUNTIME_B)
    assert isolated == dbt_project_dir / f"dex-runtime-{_RUNTIME_B}"
    _write_success(isolated)

    evidence = build_evidence(
        dbt_project_dir, target="dev", artifact_directory=isolated
    )

    assert evidence.outcome is BuildOutcome.VALIDATED
    producer = evidence.invocation["artifact_producer"]
    assert producer["artifact_directory"] == f"dex-runtime-{_RUNTIME_B}"
    assert producer["invocation_id"] == "abc-123"
    assert planted.is_dir()
    assert (planted / "nested.sql").is_file()


def test_same_runtime_reuses_a_clean_shared_target(tmp_path: Path):
    project = tmp_path / "proj"
    shared = project / "target"
    shared.mkdir(parents=True)
    (shared / "manifest.json").write_text("{}\n", encoding="utf-8")

    chosen = prepare_artifact_dir(project, _RUNTIME_B)

    assert chosen == shared
    assert not (project / f"dex-runtime-{_RUNTIME_B}").exists()
    assert (shared / "manifest.json").is_file()


def test_absent_artifacts_are_not_a_successful_completion(dbt_project_dir: Path):
    # The shared target would validate on its own. The empty directory is what
    # has to win, or this passes while ``artifact_directory`` is ignored.
    write_run_results(dbt_project_dir, [node("mart")])
    write_manifest_nodes(
        dbt_project_dir,
        {
            "model.dex_test.mart": {
                "name": "mart",
                "resource_type": "model",
                "relation_name": '"dev"."main"."mart"',
                "config": {"materialized": "view"},
            }
        },
    )
    empty = dbt_project_dir / "empty-artifacts"
    empty.mkdir()

    evidence = build_evidence(dbt_project_dir, target="dev", artifact_directory=empty)

    assert evidence.outcome is BuildOutcome.NOT_RUN


def test_interrupted_shared_build_is_not_this_runtime_success(dbt_project_dir: Path):
    shared = dbt_project_dir / "target"
    planted = _plant_directory_as_sql(shared)
    write_run_results(dbt_project_dir, [node("mart")], invocation_id="interrupted-run")
    write_manifest_nodes(
        dbt_project_dir,
        {
            "model.dex_test.mart": {
                "name": "mart",
                "resource_type": "model",
                "relation_name": '"dev"."main"."mart"',
                "config": {"materialized": "view"},
            }
        },
    )

    isolated = prepare_artifact_dir(dbt_project_dir, "dbt-1.12.6")
    evidence = build_evidence(
        dbt_project_dir, target="dev", artifact_directory=isolated
    )
    shared_evidence = build_evidence(
        dbt_project_dir, target="dev", artifact_directory=shared
    )

    assert isolated != shared
    assert evidence.outcome is BuildOutcome.NOT_RUN
    assert shared_evidence.outcome is BuildOutcome.VALIDATED
    assert shared_evidence.invocation["invocation_id"] == "interrupted-run"
    assert planted.is_dir()
    assert (shared / "run_results.json").is_file()


def test_build_passes_isolated_target_path_when_the_shared_dir_collides(
    dbt_project_dir: Path,
):
    shared = dbt_project_dir / "target"
    planted = _plant_directory_as_sql(shared)
    (shared / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"invocation_id": "stale-shared"},
                "results": [node("mart")],
                "elapsed_time": 4.0,
            }
        ),
        encoding="utf-8",
    )
    isolated = dbt_project_dir / f"dex-runtime-{_RUNTIME_B}"
    isolated.mkdir()
    (isolated / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"invocation_id": "stale-isolated"},
                "results": [node("mart", status="error")],
                "elapsed_time": 9.0,
            }
        ),
        encoding="utf-8",
    )
    (isolated / "manifest.json").write_text("{}\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        seen["isolated_results"] = (isolated / "run_results.json").exists()
        seen["shared_results"] = (shared / "run_results.json").is_file()
        seen["isolated_manifest"] = (isolated / "manifest.json").is_file()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=runner,
        runtime_key=_RUNTIME_B,
    )

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert Path(argv[argv.index("--target-path") + 1]) == isolated
    assert seen["isolated_results"] is False
    assert seen["shared_results"] is True
    assert seen["isolated_manifest"] is True
    assert planted.is_dir()
    assert summary["success"] is True
    assert summary["nodes"] == []
    assert "elapsed_seconds" not in summary
    assert summary["artifact_directory"] == f"dex-runtime-{_RUNTIME_B}"


def test_explicit_artifact_directory_is_passed_as_target_path(
    dbt_project_dir: Path,
):
    custom = dbt_project_dir / "ci-artifacts"
    seen: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=runner,
        artifact_directory=custom,
    )

    argv = seen["argv"]
    assert Path(argv[argv.index("--target-path") + 1]) == custom
    assert summary["artifact_directory"] == "ci-artifacts"
    assert summary["nodes"] == []


def test_default_build_does_not_override_target_path(dbt_project_dir: Path):
    seen: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    summary, _cost = build(dbt_project_dir, target="dev", confirmed=True, runner=runner)

    assert "--target-path" not in seen["argv"]
    assert "artifact_directory" not in summary


def test_foreign_invocation_cannot_be_a_successful_completion(
    dbt_project_dir: Path,
):
    write_run_results(dbt_project_dir, [node("mart")], invocation_id="invocation-a")

    with pytest.raises(ForeignInvocationError, match="invocation-b"):
        build_evidence(
            dbt_project_dir,
            target="dev",
            expected_invocation_id="invocation-b",
        )

    evidence = build_evidence(
        dbt_project_dir,
        target="dev",
        expected_invocation_id="invocation-a",
    )
    assert evidence.outcome is BuildOutcome.VALIDATED
    assert evidence.invocation["invocation_id"] == "invocation-a"


def test_assert_same_invocation_allows_a_missing_side():
    assert_same_invocation(None, None)
    assert_same_invocation("invocation-a", None)
    assert_same_invocation(None, "invocation-a")
    assert_same_invocation("invocation-a", "invocation-a")
    with pytest.raises(ForeignInvocationError, match="invocation-b"):
        assert_same_invocation("invocation-a", "invocation-b")
