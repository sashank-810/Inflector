"""Repository-owned Linux installation and final launch-gate contracts."""

from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = subprocess.run(
    ("git", "rev-parse", "HEAD"),
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()


def _deployment() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runpy.run_path(str(ROOT / "scripts/inflector_linux_deploy.py")),
    )


def _gate() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runpy.run_path(str(ROOT / "scripts/production_launch_gate.py")),
    )


def _operator() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runpy.run_path(str(ROOT / "scripts/inflector_production_operator.py")),
    )


def _completed(command: tuple[str, ...], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_sha_validation_rejects_malformed_values() -> None:
    module = _deployment()
    assert module["validate_full_sha"](BASE_SHA) == BASE_SHA
    for value in ("latest", BASE_SHA[:-1], BASE_SHA.upper(), "g" * 40, f"{BASE_SHA}\n"):
        with pytest.raises(module["DeploymentError"], match="40 lowercase"):
            module["validate_full_sha"](value)


def test_reviewed_source_requires_exact_head_and_clean_tree(tmp_path: Path) -> None:
    module = _deployment()
    commands: list[tuple[str, ...]] = []

    def matching_runner(command: tuple[str, ...], **_: object) -> SimpleNamespace:
        commands.append(command)
        if command[1:3] == ("rev-parse", "HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{BASE_SHA}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module["validate_source_checkout"](tmp_path, BASE_SHA, runner=matching_runner)
    assert ("git", "status", "--porcelain", "--untracked-files=all") in commands

    def foreign_runner(command: tuple[str, ...], **_: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=f"{'0' * 40}\n", stderr="")

    with pytest.raises(module["DeploymentError"], match="does not equal"):
        module["validate_source_checkout"](tmp_path, BASE_SHA, runner=foreign_runner)


def test_host_directory_bootstrap_is_idempotent_and_conservative(tmp_path: Path) -> None:
    module = _deployment()
    layout = module["HostLayout"](tmp_path)
    module["prepare_host_directories"](
        layout,
        runtime_uid=1001,
        runtime_gid=1001,
        apply_ownership=False,
    )
    module["prepare_host_directories"](
        layout,
        runtime_uid=1001,
        runtime_gid=1001,
        apply_ownership=False,
    )
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "etc/inflector").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "opt/inflector/releases").stat().st_mode) == 0o755
        assert stat.S_IMODE((tmp_path / "srv/inflector/archive/nse").stat().st_mode) == 0o750
        assert stat.S_IMODE((tmp_path / "srv/inflector/archive/gdelt").stat().st_mode) == 0o750
    else:
        source = (ROOT / "scripts/inflector_linux_deploy.py").read_text(encoding="utf-8")
        assert '("/etc/inflector", 0, 0, 0o700)' in source
        assert '("/srv/inflector/archive/nse", runtime_uid, runtime_gid, 0o750)' in source
    assert not (tmp_path / "opt/inflector/current").exists()


def test_release_only_preflight_validates_exact_sha_and_assets(tmp_path: Path) -> None:
    module = _deployment()
    for relative in module["RELEASE_ASSETS"]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source = ROOT / relative
        target.write_bytes(source.read_bytes())
    for relative in (".venv/bin/python", "apps/web/.next/BUILD_ID"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("present", encoding="utf-8")
    (tmp_path / "apps/web/.next/cache").mkdir(parents=True)

    def runner(command: tuple[str, ...], **_: object) -> SimpleNamespace:
        stdout = f"{BASE_SHA}\n" if command[1:3] == ("rev-parse", "HEAD") else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    result = module["production_preflight"](
        expected_sha=BASE_SHA,
        release=tmp_path,
        release_only=True,
        runner=runner,
    )
    assert result["status"] == "pass"
    assert result["checks"]["release_policy_checksum"] is True


def test_systemd_installer_validates_units_and_never_enables_timer(tmp_path: Path) -> None:
    module = _deployment()
    release = tmp_path / "release"
    source_units = release / "deploy/systemd"
    source_units.mkdir(parents=True)
    for name in module["UNIT_NAMES"]:
        (source_units / name).write_text(
            (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    caddy = release / "deploy/caddy"
    caddy.mkdir(parents=True)
    (caddy / module["CADDY_DROP_IN"]).write_text(
        (ROOT / "deploy/caddy" / module["CADDY_DROP_IN"]).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    destination = tmp_path / "systemd"
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **_: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module["install_systemd_units"](
        release=release,
        enable_api=True,
        enable_web=True,
        runner=runner,
        destination=destination,
        require_root=False,
    )
    assert all((destination / name).is_file() for name in module["UNIT_NAMES"])
    assert (destination / "caddy.service.d/inflector.conf").is_file()
    assert ("systemctl", "daemon-reload") in commands
    assert ("systemctl", "enable", "--now", "inflector-api.service") in commands
    assert ("systemctl", "enable", "--now", "inflector-web.service") in commands
    assert not any(
        "inflector-cycle.timer" in command and "enable" in command for command in commands
    )


def test_runtime_readiness_reuses_clis_in_process_and_does_not_spawn_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _deployment()
    import inflector_data.model_release_cli as release_cli
    import inflector_data.ops_cli as ops_cli

    secret = "postgresql+psycopg://operator:do-not-expose@example/inflector"
    accepted: list[tuple[str, ...]] = []

    def fake_health(arguments: list[str]) -> int:
        accepted.append(tuple(arguments))
        print(json.dumps({"status": "ready"}))
        return 0

    def fake_release(arguments: list[str]) -> int:
        accepted.append(tuple(arguments))
        print(
            json.dumps(
                {
                    "status": "ready",
                    "active_subject": "champion",
                    "historical_model_validation": "data_blocked",
                }
            )
        )
        return 0

    monkeypatch.setattr(ops_cli, "main", fake_health)
    monkeypatch.setattr(release_cli, "main", fake_release)
    result = module["_runtime_readiness"](
        tmp_path,
        {
            "INFLECTOR_PRODUCTION_DATABASE_URL": secret,
            "INFLECTOR_PRODUCTION_RAW_ROOT": "/srv/inflector/archive/nse",
            "INFLECTOR_GDELT_RAW_ROOT": "/srv/inflector/archive/gdelt",
            "INFLECTOR_MODEL_FAMILY": "inflector_v1",
        },
    )
    assert result["release_readiness_status"] == "ready"
    assert len(accepted) == 2
    assert secret in accepted[1]
    assert not any(command[0] in {"python", "python3", sys.executable} for command in accepted)


def test_operator_bridge_keeps_database_secret_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _operator()
    import inflector_data.model_release_cli as release_cli

    secret = "postgresql+psycopg://operator:never-print@example/inflector"
    accepted: list[str] = []

    def fake_main(arguments: list[str]) -> int:
        accepted.extend(arguments)
        print(json.dumps({"status": "ready", "deployment_id": "safe-id", "subject": "champion"}))
        return 0

    monkeypatch.setattr(release_cli, "main", fake_main)
    args = SimpleNamespace(command="bootstrap-champion", activated_at="2026-10-19T20:00:00+05:30")
    result = module["execute_operator_command"](
        args,
        environment={"INFLECTOR_PRODUCTION_DATABASE_URL": secret},
        release=ROOT,
    )
    assert accepted[accepted.index("--database-url") + 1] == secret
    assert result == {
        "status": "ready",
        "deployment_id": "safe-id",
        "subject": "champion",
        "operator_command": "bootstrap-champion",
        "exit_code": 0,
    }
    output = capsys.readouterr()
    assert secret not in output.out
    assert secret not in output.err


def test_operator_bridge_refuses_database_argument_from_operator() -> None:
    module = _operator()
    with pytest.raises(module["OperatorCommandError"], match="root-only environment"):
        module["_with_database"](
            ("ingest-universe", "--database-url", "forbidden"),
            "memory-only",
        )


def test_operator_bridge_does_not_expose_unbound_research_or_historical_ingestion() -> None:
    module = _operator()
    assert "run-current" not in module["ALLOWED_RESEARCH_COMMANDS"]
    assert "ingest-historical-mii" not in module["ALLOWED_NSE_COMMANDS"]
    assert module["ALLOWED_RESEARCH_COMMANDS"] == {"ingest-gdelt-news", "init-model"}


def test_launch_gate_fails_closed_without_evidence() -> None:
    module = _gate()
    result = module["decide_launch"](
        expected_sha=BASE_SHA,
        preflight={"status": "blocked", "expected_sha": BASE_SHA, "blocker_class": "environment"},
        attestations={"reviewed_git_sha": BASE_SHA},
    )
    assert result["status"] == "NO_GO_CODE_OR_CI"
    assert result["automatic_side_effects_performed"] is False


def test_launch_gate_requires_every_operator_attestation_for_go() -> None:
    module = _gate()
    attestations = {
        "reviewed_git_sha": BASE_SHA,
        "github_ci_run_id": "reviewed-ci-run",
        **{name: True for name in module["CODE_ATTESTATIONS"]},
        **{name: True for name in module["ENVIRONMENT_ATTESTATIONS"]},
    }
    result = module["decide_launch"](
        expected_sha=BASE_SHA,
        preflight={"status": "pass", "expected_sha": BASE_SHA, "blocker_class": None},
        attestations=attestations,
    )
    assert result["status"] == "GO_FOR_CONTROLLED_PRODUCTION_LAUNCH"
    assert result["recurring_timer_automatically_enabled"] is False
    attestations["telegram_single_message_smoke_passed"] = False
    blocked = module["decide_launch"](
        expected_sha=BASE_SHA,
        preflight={"status": "pass", "expected_sha": BASE_SHA, "blocker_class": None},
        attestations=attestations,
    )
    assert blocked["status"] == "NO_GO_ENVIRONMENT"


def test_launch_gate_rejects_mismatched_identity_as_integrity_failure() -> None:
    module = _gate()
    result = module["decide_launch"](
        expected_sha=BASE_SHA,
        preflight={"status": "pass", "expected_sha": "0" * 40},
        attestations={"reviewed_git_sha": BASE_SHA, "github_ci_success": True},
    )
    assert result["status"] == "NO_GO_INTEGRITY"


def test_tooling_contains_no_implicit_latest_or_secret_process_arguments() -> None:
    source = (ROOT / "scripts/inflector_linux_deploy.py").read_text(encoding="utf-8")
    assert "--sha" in source
    assert "latest" not in source.lower()
    assert '("systemctl", "enable", "--now", "inflector-cycle.timer")' not in source
    assert "--database-url" in source  # only an in-process accepted CLI argument
    assert "model_release_cli.main" in source
    assert "ops_cli.main" in source


def test_activation_runbook_separates_operator_actions_and_machine_gates() -> None:
    runbook = (ROOT / "docs/production-activation-runbook.md").read_text(
        encoding="utf-8"
    )
    assert "Operator-only prerequisites" in runbook
    assert "inflector_linux_deploy.py bootstrap-host" in runbook
    assert "install-release" in runbook
    assert "migration-heads" in runbook
    assert "research init-model" in runbook
    assert "bootstrap-champion" in runbook
    assert "historical_model_validation=data_blocked" in runbook
    assert "transport_unavailable" in runbook
    assert "/etc/inflector/caddy.env" in runbook
    assert "telegram-smoke --delivery-id" in runbook
    assert "GO_FOR_CONTROLLED_PRODUCTION_LAUNCH" in runbook
    final_gate = runbook.index("Only `GO_FOR_CONTROLLED_PRODUCTION_LAUNCH`")
    timer_enable = runbook.index("systemctl enable --now inflector-cycle.timer")
    assert final_gate < timer_enable
    assert "password" not in runbook.lower()


def test_launch_attestation_template_defaults_every_gate_to_false() -> None:
    values = json.loads(
        (ROOT / "deploy/launch-attestations.example.json").read_text(encoding="utf-8")
    )
    assert values["reviewed_git_sha"] == ""
    assert values["github_ci_run_id"] == ""
    for name in (*_gate()["CODE_ATTESTATIONS"], *_gate()["ENVIRONMENT_ATTESTATIONS"]):
        assert values[name] is False


def test_caddy_runtime_environment_is_public_and_service_owned() -> None:
    template = (ROOT / "deploy/env/inflector-caddy.env.example").read_text(
        encoding="utf-8"
    )
    drop_in = (ROOT / "deploy/caddy/inflector-caddy.service.conf").read_text(
        encoding="utf-8"
    )
    assert "INFLECTOR_PUBLIC_API_HOST=" in template
    assert "INFLECTOR_PUBLIC_WEB_HOST=" in template
    assert "DATABASE_URL" not in template
    assert "TELEGRAM" not in template
    assert "EnvironmentFile=/etc/inflector/caddy.env" in drop_in
