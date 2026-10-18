"""Native Linux entry point for one accepted scheduled production cycle."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from inflector_data import ops_cli
from inflector_data.operations_profile import load_operations_profile
from inflector_data.research_profile import load_research_profile

OPERATIONS_PROFILE_CODE = "nse_daily_operations_v6"
OPERATIONS_PROFILE_VERSION = "6"
OPERATIONS_PROFILE_CHECKSUM = "f71631f7f5baddd42796aae67c42c2732b1435a4d0a13c22ebe06031e70ae41f"
RESEARCH_PROFILE_CODE = "nse_current_research_v4"
RESEARCH_PROFILE_VERSION = "4"
RESEARCH_PROFILE_CHECKSUM = "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
TIMEZONE = "Asia/Kolkata"
SCHEDULED_LOCAL_TIME = "20:00"

REQUIRED_ENVIRONMENT = (
    "INFLECTOR_PRODUCTION_DATABASE_URL",
    "INFLECTOR_PRODUCTION_RAW_ROOT",
    "INFLECTOR_NSE_LICENSE_CLASS",
    "INFLECTOR_GDELT_RAW_ROOT",
    "INFLECTOR_GDELT_LICENSE_CLASS",
    "INFLECTOR_MODEL_FAMILY",
    "INFLECTOR_RESEARCH_PROFILE",
    "INFLECTOR_OPERATIONS_PROFILE",
    "INFLECTOR_SYMBOLS_FILE",
    "INFLECTOR_MODEL_SEMANTIC_VERSION",
    "INFLECTOR_DEPLOYED_GIT_SHA",
    "INFLECTOR_MODEL_EFFECTIVE_FROM",
)


class ScheduledCycleConfigurationError(RuntimeError):
    """The immutable Linux production-cycle environment is incomplete or foreign."""


def build_cycle_arguments(
    environment: Mapping[str, str],
    *,
    repository_root: Path,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Validate deployment identity and construct accepted in-process CLI arguments."""

    values = {name: _required(environment, name) for name in REQUIRED_ENVIRONMENT}
    root = repository_root.resolve()
    operations_path = _inside_repository(root, values["INFLECTOR_OPERATIONS_PROFILE"])
    research_path = _inside_repository(root, values["INFLECTOR_RESEARCH_PROFILE"])
    operations = load_operations_profile(operations_path)
    research = load_research_profile(research_path)
    if (
        operations.operations_profile_code != OPERATIONS_PROFILE_CODE
        or operations.profile_version != OPERATIONS_PROFILE_VERSION
        or operations.checksum_sha256 != OPERATIONS_PROFILE_CHECKSUM
        or operations.timezone != TIMEZONE
        or operations.scheduled_local_time != SCHEDULED_LOCAL_TIME
        or not operations.automatic_financial_endpoint
        or not operations.gdelt_enabled
    ):
        raise ScheduledCycleConfigurationError("accepted Operations V6 profile is required")
    if (
        research.research_profile_code != RESEARCH_PROFILE_CODE
        or research.profile_version != RESEARCH_PROFILE_VERSION
        or research.checksum_sha256 != RESEARCH_PROFILE_CHECKSUM
    ):
        raise ScheduledCycleConfigurationError("accepted Research V4 profile is required")

    deployed_sha = values["INFLECTOR_DEPLOYED_GIT_SHA"]
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise ScheduledCycleConfigurationError("deployed Git SHA must be a full lowercase SHA-1")
    _verify_deployed_sha(root, deployed_sha)
    effective_from = _aware_iso8601(
        values["INFLECTOR_MODEL_EFFECTIVE_FROM"], "INFLECTOR_MODEL_EFFECTIVE_FROM"
    )
    clock = now or datetime.now(ZoneInfo(TIMEZONE))
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ScheduledCycleConfigurationError("scheduler clock must be timezone-aware")
    cycle_at = clock.astimezone(ZoneInfo(TIMEZONE)).isoformat()

    return (
        "run-cycle-auto",
        "--database-url",
        values["INFLECTOR_PRODUCTION_DATABASE_URL"],
        "--operations-profile",
        str(operations_path),
        "--research-profile",
        str(research_path),
        "--model-family",
        values["INFLECTOR_MODEL_FAMILY"],
        "--symbols-file",
        values["INFLECTOR_SYMBOLS_FILE"],
        "--cycle-at",
        cycle_at,
        "--raw-root",
        values["INFLECTOR_PRODUCTION_RAW_ROOT"],
        "--nse-license-class",
        values["INFLECTOR_NSE_LICENSE_CLASS"],
        "--gdelt-raw-root",
        values["INFLECTOR_GDELT_RAW_ROOT"],
        "--gdelt-license-class",
        values["INFLECTOR_GDELT_LICENSE_CLASS"],
        "--model-semantic-version",
        values["INFLECTOR_MODEL_SEMANTIC_VERSION"],
        "--git-sha",
        deployed_sha,
        "--model-effective-from",
        effective_from.isoformat(),
    )


def run_scheduled_cycle(
    environment: Mapping[str, str],
    *,
    repository_root: Path,
    now: datetime | None = None,
) -> int:
    """Execute one accepted cycle in-process so secrets never enter child argv."""

    arguments = build_cycle_arguments(
        environment,
        repository_root=repository_root,
        now=now,
    )
    return ops_cli.main(arguments)


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise ScheduledCycleConfigurationError("the scheduled wrapper accepts no arguments")
    if not sys.platform.startswith("linux"):
        raise ScheduledCycleConfigurationError("the scheduled wrapper is Linux-only")
    repository_root = Path(__file__).resolve().parents[1]
    return run_scheduled_cycle(os.environ, repository_root=repository_root)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value or value != value.strip():
        raise ScheduledCycleConfigurationError(f"required environment variable is missing: {name}")
    return value


def _inside_repository(repository_root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = (
        (repository_root / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    try:
        resolved.relative_to(repository_root)
    except ValueError as error:
        raise ScheduledCycleConfigurationError(
            "profile path must remain inside the release"
        ) from error
    return resolved


def _aware_iso8601(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ScheduledCycleConfigurationError(f"{field} must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScheduledCycleConfigurationError(f"{field} must be timezone-aware")
    return parsed


def _verify_deployed_sha(repository_root: Path, intended_sha: str) -> None:
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ScheduledCycleConfigurationError(
            "deployed Git identity cannot be verified"
        ) from error
    observed = completed.stdout.strip()
    if completed.returncode != 0 or observed != intended_sha:
        raise ScheduledCycleConfigurationError("deployed Git SHA does not match the release")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ScheduledCycleConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
