"""Native Linux deployment contracts for the accepted production cycle."""

from __future__ import annotations

import runpy
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = subprocess.run(
    ("git", "rev-parse", "HEAD"),
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()


def _wrapper() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        runpy.run_path(str(ROOT / "scripts/run_inflector_scheduled_linux.py")),
    )


def _environment() -> dict[str, str]:
    return {
        "INFLECTOR_PRODUCTION_DATABASE_URL": "postgresql+psycopg://secret-value",
        "INFLECTOR_PRODUCTION_RAW_ROOT": "/srv/inflector/archive/nse",
        "INFLECTOR_NSE_LICENSE_CLASS": "operator-reviewed",
        "INFLECTOR_GDELT_RAW_ROOT": "/srv/inflector/archive/gdelt",
        "INFLECTOR_GDELT_LICENSE_CLASS": "operator-reviewed",
        "INFLECTOR_MODEL_FAMILY": "inflector_v1",
        "INFLECTOR_RESEARCH_PROFILE": "config/research/production_research_v4.json",
        "INFLECTOR_OPERATIONS_PROFILE": "config/operations/production_operations_v6.json",
        "INFLECTOR_SYMBOLS_FILE": "/srv/inflector/config/production-symbols.txt",
        "INFLECTOR_MODEL_SEMANTIC_VERSION": "1.0.0",
        "INFLECTOR_DEPLOYED_GIT_SHA": BASE_SHA,
        "INFLECTOR_MODEL_EFFECTIVE_FROM": "2026-10-19T00:00:00+05:30",
    }


def test_linux_wrapper_requires_every_immutable_input_without_exposing_secrets() -> None:
    module = _wrapper()
    environment = _environment()
    secret = environment["INFLECTOR_PRODUCTION_DATABASE_URL"]
    environment.pop("INFLECTOR_MODEL_EFFECTIVE_FROM")
    with pytest.raises(module["ScheduledCycleConfigurationError"]) as captured:
        module["build_cycle_arguments"](environment, repository_root=ROOT)
    assert "INFLECTOR_MODEL_EFFECTIVE_FROM" in str(captured.value)
    assert secret not in str(captured.value)


def test_linux_wrapper_uses_accepted_profiles_kolkata_time_and_deployed_sha() -> None:
    module = _wrapper()
    arguments = module["build_cycle_arguments"](
        _environment(),
        repository_root=ROOT,
        now=datetime(2026, 10, 19, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    assert arguments[0] == "run-cycle-auto"
    assert arguments[arguments.index("--cycle-at") + 1] == "2026-10-19T20:00:00+05:30"
    assert arguments[arguments.index("--git-sha") + 1] == BASE_SHA
    assert arguments[arguments.index("--model-semantic-version") + 1] == "1.0.0"
    operations_path = arguments[arguments.index("--operations-profile") + 1].replace(
        "\\", "/"
    )
    research_path = arguments[arguments.index("--research-profile") + 1].replace(
        "\\", "/"
    )
    assert operations_path.endswith("config/operations/production_operations_v6.json")
    assert research_path.endswith("config/research/production_research_v4.json")


def test_linux_wrapper_rejects_foreign_deployed_sha() -> None:
    module = _wrapper()
    environment = _environment()
    environment["INFLECTOR_DEPLOYED_GIT_SHA"] = "0" * 40
    with pytest.raises(module["ScheduledCycleConfigurationError"], match="does not match"):
        module["build_cycle_arguments"](environment, repository_root=ROOT)


def test_scheduler_executes_ops_in_process_without_secrets_in_child_argv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _wrapper()
    environment = _environment()
    database_url = environment["INFLECTOR_PRODUCTION_DATABASE_URL"]
    environment["INFLECTOR_TELEGRAM_BOT_TOKEN"] = "telegram-secret-token"
    environment["INFLECTOR_TELEGRAM_CHAT_ID"] = "telegram-secret-chat"
    child_commands: list[tuple[str, ...]] = []
    accepted_arguments: list[tuple[str, ...]] = []

    def fake_subprocess_run(command: tuple[str, ...], **_: object) -> SimpleNamespace:
        child_commands.append(tuple(command))
        return SimpleNamespace(returncode=0, stdout=f"{BASE_SHA}\n")

    def fake_ops_main(arguments: tuple[str, ...]) -> int:
        accepted_arguments.append(tuple(arguments))
        return 7

    monkeypatch.setattr(module["subprocess"], "run", fake_subprocess_run)
    monkeypatch.setattr(module["ops_cli"], "main", fake_ops_main)

    exit_code = module["run_scheduled_cycle"](
        environment,
        repository_root=ROOT,
        now=datetime(2026, 10, 19, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert exit_code == 7
    assert child_commands == [("git", "rev-parse", "HEAD")]
    assert len(accepted_arguments) == 1
    arguments = accepted_arguments[0]
    assert arguments[arguments.index("--database-url") + 1] == database_url
    assert arguments[arguments.index("--git-sha") + 1] == BASE_SHA
    serialized_child_argv = repr(child_commands)
    assert database_url not in serialized_child_argv
    assert environment["INFLECTOR_TELEGRAM_BOT_TOKEN"] not in serialized_child_argv
    assert environment["INFLECTOR_TELEGRAM_CHAT_ID"] not in serialized_child_argv
    output = capsys.readouterr()
    assert database_url not in output.out
    assert database_url not in output.err


def test_scheduler_failure_does_not_interpolate_database_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _wrapper()
    environment = _environment()
    database_url = environment["INFLECTOR_PRODUCTION_DATABASE_URL"]

    def fake_ops_main(_: tuple[str, ...]) -> int:
        raise RuntimeError("accepted operations entry point failed")

    monkeypatch.setattr(module["ops_cli"], "main", fake_ops_main)
    with pytest.raises(RuntimeError) as captured:
        module["run_scheduled_cycle"](environment, repository_root=ROOT)

    assert database_url not in str(captured.value)
    output = capsys.readouterr()
    assert database_url not in output.out
    assert database_url not in output.err


def test_systemd_units_are_loopback_native_and_timer_is_explicit() -> None:
    units = ROOT / "deploy/systemd"
    api = (units / "inflector-api.service").read_text(encoding="utf-8")
    web = (units / "inflector-web.service").read_text(encoding="utf-8")
    cycle = (units / "inflector-cycle.service").read_text(encoding="utf-8")
    timer = (units / "inflector-cycle.timer").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8000" in api
    assert "--hostname 127.0.0.1 --port 3000" in web
    assert "run_inflector_scheduled_linux.py" in cycle
    assert "run_inflector_scheduled.ps1" not in cycle
    assert "Type=oneshot" in cycle
    assert "TimeoutStartSec=4h" in cycle
    assert "OnCalendar=*-*-* 20:00:00 Asia/Kolkata" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec" not in timer


def test_systemd_units_use_service_specific_environment_files() -> None:
    units = ROOT / "deploy/systemd"
    api = (units / "inflector-api.service").read_text(encoding="utf-8")
    web = (units / "inflector-web.service").read_text(encoding="utf-8")
    cycle = (units / "inflector-cycle.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/inflector/api.env" in api
    assert "EnvironmentFile=/etc/inflector/web.env" in web
    assert "EnvironmentFile=/etc/inflector/cycle.env" in cycle
    assert "/etc/inflector/inflector.env" not in api + web + cycle
    assert "INFLECTOR_TELEGRAM" not in api + web
    assert "DATABASE_URL" not in web


def test_service_environment_templates_enforce_least_privilege() -> None:
    templates = ROOT / "deploy/env"
    api = (templates / "inflector-api.env.example").read_text(encoding="utf-8")
    web = (templates / "inflector-web.env.example").read_text(encoding="utf-8")
    cycle = (templates / "inflector-cycle.env.example").read_text(encoding="utf-8")
    assert "DATABASE_URL=" in api
    assert "INFLECTOR_TELEGRAM_BOT_TOKEN" not in api
    assert "INFLECTOR_TELEGRAM_CHAT_ID" not in api
    assert "DATABASE_URL" not in web
    assert "INFLECTOR_PRODUCTION_DATABASE_URL" not in web
    assert "INFLECTOR_TELEGRAM_BOT_TOKEN" not in web
    assert "INFLECTOR_TELEGRAM_CHAT_ID" not in web

    cycle_values = {
        key: value
        for line in cycle.splitlines()
        if line and not line.startswith("#")
        for key, value in [line.split("=", 1)]
    }
    assert cycle_values["INFLECTOR_PRODUCTION_DATABASE_URL"] == ""
    assert cycle_values["INFLECTOR_TELEGRAM_BOT_TOKEN"] == ""
    assert cycle_values["INFLECTOR_TELEGRAM_CHAT_ID"] == ""


def test_frontend_api_origin_is_build_time_and_cache_is_narrowly_writable() -> None:
    runbook = (ROOT / "docs/production-linux-deployment.md").read_text(encoding="utf-8")
    web_unit = (ROOT / "deploy/systemd/inflector-web.service").read_text(
        encoding="utf-8"
    )
    web_template = (ROOT / "deploy/env/inflector-web.env.example").read_text(
        encoding="utf-8"
    )
    web_keys = {
        line.split("=", 1)[0]
        for line in web_template.splitlines()
        if line and not line.startswith("#")
    }
    assert 'NEXT_PUBLIC_API_BASE_URL="$NEXT_PUBLIC_API_BASE_URL" npm run build' in runbook
    assert "NEXT_PUBLIC_API_BASE_URL" not in web_keys
    assert "ReadOnlyPaths=/opt/inflector/current" in web_unit
    assert "ReadWritePaths=/opt/inflector/current/apps/web/.next/cache" in web_unit
    assert "install -d -o inflector -g inflector" in runbook
    assert "sole runtime-writable exception" in runbook


def test_production_environment_template_contains_no_secret_values() -> None:
    template = (ROOT / "deploy/inflector-production.env.example").read_text(encoding="utf-8")
    values = {
        key: value
        for line in template.splitlines()
        if line and not line.startswith("#")
        for key, value in [line.split("=", 1)]
    }
    assert values["INFLECTOR_PRODUCTION_DATABASE_URL"] == ""
    assert values["DATABASE_URL"] == ""
    assert values["INFLECTOR_TELEGRAM_BOT_TOKEN"] == ""
    assert values["INFLECTOR_TELEGRAM_CHAT_ID"] == ""
    assert values["INFLECTOR_DEPLOYED_GIT_SHA"] == ""
    assert values["INFLECTOR_MODEL_EFFECTIVE_FROM"] == ""
    assert values["INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE"] == "0"


def test_windows_scheduler_requires_parameters_and_has_a_windows_python_default() -> None:
    source = (ROOT / "scripts/run_inflector_scheduled.ps1").read_text(encoding="utf-8")
    for parameter in (
        "OperationsProfile",
        "ResearchProfile",
        "ModelFamily",
        "SymbolsFile",
        "ModelSemanticVersion",
        "GitSha",
        "ModelEffectiveFrom",
    ):
        assert f"[string]${parameter}" in source
    assert ".\\.venv\\Scripts\\python.exe" in source
