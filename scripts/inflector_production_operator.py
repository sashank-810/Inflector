"""Secret-safe operator bridge to accepted production command entry points.

The root-only cycle environment is read into process memory. Database credentials
are supplied to accepted CLI ``main`` functions in process and never placed in a
spawned process argument vector. This bridge intentionally exposes no T or
challenger operation.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import stat
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

DEFAULT_ENVIRONMENT_FILE = Path("/etc/inflector/cycle.env")
DEFAULT_RELEASE = Path("/opt/inflector/current")
ALLOWED_NSE_COMMANDS = frozenset(
    {
        "ingest-universe",
        "ingest-market",
        "ingest-market-range",
        "ingest-delivery-range",
        "ingest-benchmarks",
        "ingest-daily",
        "ingest-financials",
        "ingest-corporate-actions",
        "ingest-announcements",
        "ingest-catalyst-evidence",
    }
)
ALLOWED_RESEARCH_COMMANDS = frozenset({"ingest-gdelt-news", "init-model"})


class OperatorCommandError(RuntimeError):
    """Raised when a deployment-only operator contract is not satisfied."""


def load_environment(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise OperatorCommandError("cycle environment file is unavailable")
    if sys.platform.startswith("linux"):
        metadata = path.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OperatorCommandError("cycle environment file must be root-owned mode 0600")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise OperatorCommandError(f"invalid cycle environment assignment on line {number}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def required(values: dict[str, str], name: str) -> str:
    value = values.get(name, "")
    if not value or value != value.strip():
        raise OperatorCommandError(f"required production environment value is absent: {name}")
    return value


def _accepted_call(
    main: Callable[[Sequence[str] | None], int], arguments: list[str]
) -> tuple[int, dict[str, Any]]:
    captured_out = io.StringIO()
    with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(io.StringIO()):
        exit_code = main(arguments)
    lines = [line for line in captured_out.getvalue().splitlines() if line.strip()]
    if not lines:
        return exit_code, {"status": "missing_output"}
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return exit_code, {"status": "invalid_output"}
    return exit_code, payload if isinstance(payload, dict) else {"status": "invalid_output"}


def _safe_result(*, command: str, exit_code: int, payload: dict[str, Any]) -> dict[str, object]:
    allowed = {
        "status",
        "run_id",
        "deployment_id",
        "subject",
        "active_subject",
        "active_model_checksum",
        "operational_status",
        "historical_model_validation",
        "challenger_activation",
        "blockers",
        "runs",
        "delivery_id",
        "delivery_key_sha256",
        "outbox_event_id",
        "outbox_event_key_sha256",
        "attempt_number",
        "result_state",
        "provider_message_id",
        "attempted_at",
        "completed_at",
        "policy_checksum_sha256",
        "release_candidate_id",
        "release_candidate_key_sha256",
        "migration_head",
    }
    result = {key: value for key, value in payload.items() if key in allowed}
    result.update({"operator_command": command, "exit_code": exit_code})
    if "status" not in result:
        result["status"] = "completed" if exit_code == 0 else "failed"
    return result


def _with_database(arguments: Sequence[str], database_url: str) -> list[str]:
    if not arguments:
        raise OperatorCommandError("an accepted CLI subcommand is required")
    if "--database-url" in arguments:
        raise OperatorCommandError("database URL is injected from the root-only environment")
    return [arguments[0], "--database-url", database_url, *arguments[1:]]


def execute_operator_command(
    args: argparse.Namespace,
    *,
    environment: dict[str, str],
    release: Path,
) -> dict[str, object]:
    database_url = required(environment, "INFLECTOR_PRODUCTION_DATABASE_URL")
    previous = os.environ.copy()
    os.environ.update(environment)
    os.environ["DATABASE_URL"] = database_url
    try:
        if args.command in {"migration-heads", "migrate", "migration-current"}:
            from alembic import command
            from alembic.config import Config

            configuration = Config(str(release / "alembic.ini"))
            configuration.set_main_option("script_location", str(release / "migrations"))
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
                if args.command == "migration-heads":
                    command.heads(configuration)
                elif args.command == "migrate":
                    command.upgrade(configuration, "head")
                else:
                    command.current(configuration)
            output = captured.getvalue()
            head_is_expected = "20261009_0028" in output or args.command == "migrate"
            code = 0 if head_is_expected else 1
            payload = {
                "status": "ready" if head_is_expected else "failed",
                "migration_head": "20261009_0028" if head_is_expected else "unexpected",
            }
        elif args.command == "nse":
            from inflector_data import nse_cli

            if not args.arguments or args.arguments[0] not in ALLOWED_NSE_COMMANDS:
                raise OperatorCommandError(
                    "NSE command is not allowed during production activation"
                )
            code, payload = _accepted_call(
                nse_cli.main, _with_database(args.arguments, database_url)
            )
        elif args.command == "research":
            from inflector_data import research_cli

            if not args.arguments or args.arguments[0] not in ALLOWED_RESEARCH_COMMANDS:
                raise OperatorCommandError(
                    "research command is not allowed during production activation"
                )
            code, payload = _accepted_call(
                research_cli.main, _with_database(args.arguments, database_url)
            )
        elif args.command == "bootstrap-champion":
            from inflector_data import model_release_cli

            code, payload = _accepted_call(
                model_release_cli.main,
                [
                    "bootstrap-active-champion",
                    "--database-url",
                    database_url,
                    "--release-policy",
                    str(release / "config/operations/production_model_release_v1.json"),
                    "--activated-at",
                    args.activated_at,
                ],
            )
        elif args.command == "release-readiness":
            from inflector_data import model_release_cli

            observed_at = datetime.now(UTC).isoformat()
            code, payload = _accepted_call(
                model_release_cli.main,
                [
                    "model-release-readiness",
                    "--database-url",
                    database_url,
                    "--release-policy",
                    str(release / "config/operations/production_model_release_v1.json"),
                    "--repository-root",
                    str(release),
                    "--observed-at",
                    observed_at,
                    "--raw-root",
                    required(environment, "INFLECTOR_PRODUCTION_RAW_ROOT"),
                    "--gdelt-raw-root",
                    required(environment, "INFLECTOR_GDELT_RAW_ROOT"),
                    "--model-family",
                    required(environment, "INFLECTOR_MODEL_FAMILY"),
                    "--t-policy",
                    str(
                        release / "config/backtest/production_multibagger_temporal_holdout_v1.json"
                    ),
                    "--s2-policy",
                    str(
                        release
                        / "config/backtest/production_multibagger_challenger_development_v1.json"
                    ),
                    "--activation-policy",
                    str(
                        release
                        / "config/backtest/production_historical_evaluation_activation_v1.json"
                    ),
                    "--split-policy",
                    str(release / "config/backtest/production_multibagger_temporal_split_v1.json"),
                ],
            )
        elif args.command == "controlled-cycle":
            from run_inflector_scheduled_linux import run_scheduled_cycle

            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
                code = run_scheduled_cycle(environment, repository_root=release)
            lines = [line for line in captured.getvalue().splitlines() if line.strip()]
            try:
                payload = json.loads(lines[-1]) if lines else {"status": "missing_output"}
            except json.JSONDecodeError:
                payload = {"status": "invalid_output"}
        elif args.command == "operations-status":
            from inflector_data import ops_cli

            code, payload = _accepted_call(
                ops_cli.main,
                [
                    "status",
                    "--database-url",
                    database_url,
                    "--last",
                    str(args.last),
                ],
            )
        else:
            from inflector_data import telegram_launch_smoke_cli

            code, payload = _accepted_call(
                telegram_launch_smoke_cli.main,
                [
                    "--database-url",
                    database_url,
                    "--delivery-id",
                    str(args.delivery_id),
                    "--repository-root",
                    str(release),
                ],
            )
        return _safe_result(command=args.command, exit_code=code, payload=payload)
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file", type=Path, default=DEFAULT_ENVIRONMENT_FILE)
    parser.add_argument("--use-process-environment", action="store_true")
    parser.add_argument("--release", type=Path, default=DEFAULT_RELEASE)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("nse", "research"):
        command = commands.add_parser(name)
        command.add_argument("arguments", nargs=argparse.REMAINDER)
    bootstrap = commands.add_parser("bootstrap-champion")
    bootstrap.add_argument("--activated-at", required=True)
    commands.add_parser("release-readiness")
    commands.add_parser("controlled-cycle")
    status = commands.add_parser("operations-status")
    status.add_argument("--last", type=int, default=1)
    commands.add_parser("migration-heads")
    commands.add_parser("migrate")
    commands.add_parser("migration-current")
    telegram = commands.add_parser("telegram-smoke")
    telegram.add_argument("--delivery-id", type=UUID, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        environment = (
            dict(os.environ)
            if args.use_process_environment
            else load_environment(args.environment_file)
        )
        result = execute_operator_command(
            args,
            environment=environment,
            release=args.release.resolve(),
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["exit_code"] == 0 else 1
    except Exception as error:  # noqa: BLE001 - sanitized operator boundary
        print(
            json.dumps(
                {"status": "failed", "error_code": type(error).__name__},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
