"""Fail-closed Linux host, release, systemd, and production preflight tooling.

This module deliberately owns deployment mechanics only. Accepted Inflector
business readiness is delegated to the existing operations and model-release
CLIs, in process, so database credentials never enter a child-process argv.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

EXPECTED_MIGRATION_HEAD = "20261009_0028"
EXPECTED_RELEASE_POLICY_CHECKSUM = (
    "7094654224d0e305ae46de8babfec4c0c5368936605b807eeca0468a15ec1691"
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
UNIT_NAMES = (
    "inflector-api.service",
    "inflector-web.service",
    "inflector-cycle.service",
    "inflector-cycle.timer",
)
ENV_FILES = {
    "api": Path("/etc/inflector/api.env"),
    "web": Path("/etc/inflector/web.env"),
    "cycle": Path("/etc/inflector/cycle.env"),
    "caddy": Path("/etc/inflector/caddy.env"),
}
CADDY_DROP_IN = "inflector-caddy.service.conf"
RELEASE_ASSETS = (
    "config/research/production_research_v4.json",
    "config/operations/production_operations_v6.json",
    "config/operations/production_model_release_v1.json",
    "scripts/run_inflector_scheduled_linux.py",
    "deploy/systemd/inflector-api.service",
    "deploy/systemd/inflector-web.service",
    "deploy/systemd/inflector-cycle.service",
    "deploy/systemd/inflector-cycle.timer",
    "deploy/caddy/Caddyfile.example",
    "deploy/caddy/inflector-caddy.service.conf",
    "deploy/env/inflector-caddy.env.example",
)


class DeploymentError(RuntimeError):
    """Raised for a fail-closed deployment contract violation."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class HostLayout:
    """Production paths, optionally rooted elsewhere for non-privileged tests."""

    root: Path = Path("/")

    def path(self, absolute: str | Path) -> Path:
        value = PurePosixPath(str(absolute).replace("\\", "/"))
        if not value.is_absolute():
            raise DeploymentError(f"deployment path must be absolute: {absolute}")
        if self.root == Path("/"):
            return Path(str(value))
        return self.root.joinpath(*value.parts[1:])

    @property
    def releases(self) -> Path:
        return self.path("/opt/inflector/releases")

    @property
    def current(self) -> Path:
        return self.path("/opt/inflector/current")


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        tuple(command),
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        # Commands executed here never contain production credentials. Do not
        # include child output because third-party tools may echo environment.
        raise DeploymentError(f"command failed with exit code {completed.returncode}: {command[0]}")
    return completed


def validate_full_sha(value: str) -> str:
    if SHA_PATTERN.fullmatch(value) is None:
        raise DeploymentError("release SHA must be exactly 40 lowercase hexadecimal characters")
    return value


def _require_linux_root() -> None:
    if not sys.platform.startswith("linux"):
        raise DeploymentError("this deployment operation is Linux-only")
    if os.geteuid() != 0:
        raise DeploymentError("this deployment operation must run as root")


def _ensure_runtime_account(runner: Runner = subprocess.run) -> tuple[int, int]:
    import grp
    import pwd

    try:
        group = grp.getgrnam("inflector")
    except KeyError:
        _run(("groupadd", "--system", "inflector"), runner=runner)
        group = grp.getgrnam("inflector")
    try:
        user = pwd.getpwnam("inflector")
    except KeyError:
        _run(
            (
                "useradd",
                "--system",
                "--gid",
                "inflector",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                "inflector",
            ),
            runner=runner,
        )
        user = pwd.getpwnam("inflector")
    if user.pw_gid != group.gr_gid:
        raise DeploymentError("existing inflector user does not use the inflector primary group")
    if Path(user.pw_shell).name not in {"nologin", "false"}:
        raise DeploymentError("existing inflector user must not have an interactive shell")
    return user.pw_uid, group.gr_gid


def prepare_host_directories(
    layout: HostLayout,
    *,
    runtime_uid: int,
    runtime_gid: int,
    apply_ownership: bool = True,
) -> None:
    specifications = (
        ("/opt/inflector", 0, 0, 0o755),
        ("/opt/inflector/releases", 0, 0, 0o755),
        ("/srv/inflector", 0, 0, 0o755),
        ("/srv/inflector/archive", 0, 0, 0o755),
        ("/srv/inflector/archive/nse", runtime_uid, runtime_gid, 0o750),
        ("/srv/inflector/archive/gdelt", runtime_uid, runtime_gid, 0o750),
        ("/srv/inflector/config", 0, runtime_gid, 0o750),
        ("/etc/inflector", 0, 0, 0o700),
    )
    for name, uid, gid, mode in specifications:
        target = layout.path(name)
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir() or target.is_symlink():
            raise DeploymentError(f"required directory is not a real directory: {name}")
        target.chmod(mode)
        if apply_ownership:
            os.chown(target, uid, gid)
    current = layout.current
    if current.exists() and not current.is_symlink():
        raise DeploymentError("/opt/inflector/current must be absent or an installer-owned symlink")


def bootstrap_host(*, runner: Runner = subprocess.run) -> None:
    _require_linux_root()
    uid, gid = _ensure_runtime_account(runner)
    prepare_host_directories(HostLayout(), runtime_uid=uid, runtime_gid=gid)


def validate_source_checkout(
    source: Path, requested_sha: str, *, runner: Runner = subprocess.run
) -> None:
    requested_sha = validate_full_sha(requested_sha)
    if not source.is_dir():
        raise DeploymentError("reviewed source directory does not exist")
    head = _run(("git", "rev-parse", "HEAD"), cwd=source, runner=runner).stdout.strip()
    if head != requested_sha:
        raise DeploymentError("reviewed source HEAD does not equal the requested release SHA")
    dirty = _run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=source,
        runner=runner,
    ).stdout.strip()
    if dirty:
        raise DeploymentError("reviewed source directory must be completely clean")
    _run(("git", "cat-file", "-e", f"{requested_sha}^{{commit}}"), cwd=source, runner=runner)


def _validate_release_tree(release: Path, expected_sha: str, *, runner: Runner) -> None:
    observed = _run(("git", "rev-parse", "HEAD"), cwd=release, runner=runner).stdout.strip()
    if observed != expected_sha:
        raise DeploymentError("installed release Git identity does not match the requested SHA")
    dirty = _run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=release,
        runner=runner,
    ).stdout.strip()
    if dirty:
        raise DeploymentError("installed release checkout is not clean")
    missing = [name for name in RELEASE_ASSETS if not (release / name).is_file()]
    if missing:
        raise DeploymentError(f"installed release is missing required asset: {missing[0]}")
    if not (release / ".venv/bin/python").is_file():
        raise DeploymentError("installed release Python environment is missing")
    if not (release / "apps/web/.next/BUILD_ID").is_file():
        raise DeploymentError("installed frontend production build is missing")
    cache = release / "apps/web/.next/cache"
    if not cache.is_dir():
        raise DeploymentError("documented frontend cache directory is missing")


def _remove_staging_directory(staging: Path, releases: Path) -> None:
    resolved = staging.resolve()
    if resolved.parent != releases.resolve() or not resolved.name.startswith(".install-"):
        raise DeploymentError("refusing to remove an unexpected staging directory")
    if staging.exists():
        shutil.rmtree(staging)


def _activate_release_symlink(release: Path, current: Path) -> None:
    if current.exists() and not current.is_symlink():
        raise DeploymentError("current release path is not a symlink")
    temporary = current.parent / f".current-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(release, target_is_directory=True)
    os.replace(temporary, current)


def install_release(
    *,
    source: Path,
    requested_sha: str,
    public_api_base_url: str,
    runner: Runner = subprocess.run,
) -> Path:
    import grp
    import pwd

    _require_linux_root()
    requested_sha = validate_full_sha(requested_sha)
    if not public_api_base_url.strip():
        raise DeploymentError(
            "NEXT_PUBLIC_API_BASE_URL must be set for the reviewed frontend build"
        )
    validate_source_checkout(source, requested_sha, runner=runner)
    layout = HostLayout()
    target = layout.releases / requested_sha
    if target.exists():
        if not all(_check_release_assets(target, requested_sha, runner).values()):
            raise DeploymentError("existing immutable release failed asset validation")
        _activate_release_symlink(target, layout.current)
        return target

    staging = layout.releases / f".install-{requested_sha}-{os.getpid()}"
    if staging.exists():
        raise DeploymentError("release staging path already exists")
    try:
        _run(
            ("git", "clone", "--local", "--no-hardlinks", str(source), str(staging)), runner=runner
        )
        _run(("git", "checkout", "--detach", requested_sha), cwd=staging, runner=runner)
        python = shutil.which("python3.12")
        if python is None:
            raise DeploymentError("python3.12 is required")
        _run((python, "-m", "venv", str(staging / ".venv")), runner=runner)
        deployed_python = staging / ".venv/bin/python"
        _run((str(deployed_python), "-m", "pip", "install", "."), cwd=staging, runner=runner)
        _run(
            (
                str(deployed_python),
                "-c",
                "import inflector_api, inflector_data, inflector_database",
            ),
            cwd=staging,
            runner=runner,
        )
        web = staging / "apps/web"
        _run(("npm", "ci"), cwd=web, runner=runner)
        build_environment = dict(os.environ)
        build_environment["NEXT_PUBLIC_API_BASE_URL"] = public_api_base_url
        _run(("npm", "run", "build"), cwd=web, environment=build_environment, runner=runner)
        cache = web / ".next/cache"
        cache.mkdir(parents=True, exist_ok=True)
        runtime_user = pwd.getpwnam("inflector")
        runtime_group = grp.getgrnam("inflector")
        os.chown(cache, runtime_user.pw_uid, runtime_group.gr_gid)
        cache.chmod(0o750)
        for path in staging.rglob("*"):
            if path == cache or cache in path.parents:
                continue
            path.chmod(path.stat().st_mode & ~stat.S_IWGRP & ~stat.S_IWOTH)
        if not all(_check_release_assets(staging, requested_sha, runner).values()):
            raise DeploymentError("prepared release failed immutable asset validation")
        staging.rename(target)
        _activate_release_symlink(target, layout.current)
        return target
    except Exception:
        _remove_staging_directory(staging, layout.releases)
        raise


def _parse_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeploymentError(f"invalid environment assignment in {path.name}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise DeploymentError(f"invalid environment key in {path.name}:{line_number}")
        values[key] = value.strip()
    return values


def _environment_file_check(path: Path) -> tuple[bool, dict[str, str]]:
    if not path.is_file():
        return False, {}
    metadata = path.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
        return False, {}
    return True, _parse_environment_file(path)


def _capture_cli(
    main: Callable[[list[str]], int], arguments: list[str]
) -> tuple[int, dict[str, Any]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        exit_code = main(arguments)
    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    if not lines:
        return exit_code, {"status": "missing_output"}
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return exit_code, {"status": "invalid_output"}
    return exit_code, payload if isinstance(payload, dict) else {"status": "invalid_output"}


@contextlib.contextmanager
def _temporary_environment(values: Mapping[str, str]):
    original = os.environ.copy()
    try:
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _runtime_readiness(release: Path, environment: Mapping[str, str]) -> dict[str, object]:
    from inflector_data import model_release_cli, ops_cli

    observed_at = datetime.now(UTC).isoformat()
    with _temporary_environment(environment):
        health_code, health = _capture_cli(
            ops_cli.main,
            ["health", "--repository-root", str(release), "--observed-at", observed_at],
        )
        readiness_code, readiness = _capture_cli(
            model_release_cli.main,
            [
                "model-release-readiness",
                "--database-url",
                environment["INFLECTOR_PRODUCTION_DATABASE_URL"],
                "--release-policy",
                str(release / "config/operations/production_model_release_v1.json"),
                "--repository-root",
                str(release),
                "--observed-at",
                observed_at,
                "--raw-root",
                environment["INFLECTOR_PRODUCTION_RAW_ROOT"],
                "--gdelt-raw-root",
                environment["INFLECTOR_GDELT_RAW_ROOT"],
                "--model-family",
                environment["INFLECTOR_MODEL_FAMILY"],
                "--t-policy",
                str(release / "config/backtest/production_multibagger_temporal_holdout_v1.json"),
                "--s2-policy",
                str(
                    release
                    / "config/backtest/production_multibagger_challenger_development_v1.json"
                ),
                "--activation-policy",
                str(
                    release / "config/backtest/production_historical_evaluation_activation_v1.json"
                ),
                "--split-policy",
                str(release / "config/backtest/production_multibagger_temporal_split_v1.json"),
            ],
        )
    return {
        "operations_health_exit_code": health_code,
        "operations_health_status": health.get("status"),
        "release_readiness_exit_code": readiness_code,
        "release_readiness_status": readiness.get("status"),
        "active_subject": readiness.get("active_subject"),
        "historical_model_validation": readiness.get("historical_model_validation"),
    }


def _check_release_assets(release: Path, expected_sha: str, runner: Runner) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    try:
        _validate_release_tree(release, expected_sha, runner=runner)
        checks["release_tree"] = True
    except DeploymentError:
        checks["release_tree"] = False
    policy_path = release / "config/operations/production_model_release_v1.json"
    try:
        policy_bytes = json.dumps(
            json.loads(policy_path.read_text(encoding="utf-8")),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        import hashlib

        checks["release_policy_checksum"] = (
            hashlib.sha256(policy_bytes).hexdigest() == EXPECTED_RELEASE_POLICY_CHECKSUM
        )
    except (OSError, ValueError, TypeError):
        checks["release_policy_checksum"] = False
    try:
        research = json.loads(
            (release / "config/research/production_research_v4.json").read_text(
                encoding="utf-8"
            )
        )
        operations = json.loads(
            (release / "config/operations/production_operations_v6.json").read_text(
                encoding="utf-8"
            )
        )
        release_policy = json.loads(policy_path.read_text(encoding="utf-8"))
        checks["accepted_research_profile"] = (
            research.get("research_profile_code") == "nse_current_research_v4"
            and research.get("profile_version") == "4"
            and research.get("scoring_policy_asset")
            == "config/scoring/production_opportunity_v5.json"
        )
        checks["accepted_operations_profile"] = (
            operations.get("operations_profile_code") == "nse_daily_operations_v6"
            and operations.get("profile_version") == "6"
            and operations.get("timezone") == "Asia/Kolkata"
            and operations.get("scheduled_local_time") == "20:00"
            and operations.get("forbid_multiple_instances") is True
        )
        checks["accepted_release_policy"] = (
            release_policy.get("model_release_policy_code") == "production_model_release_v1"
        )
    except (OSError, ValueError, TypeError, AttributeError):
        checks["accepted_research_profile"] = False
        checks["accepted_operations_profile"] = False
        checks["accepted_release_policy"] = False
    return checks


def production_preflight(
    *,
    expected_sha: str,
    release: Path,
    release_only: bool,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    expected_sha = validate_full_sha(expected_sha)
    checks = _check_release_assets(release, expected_sha, runner)
    details: dict[str, object] = {"expected_sha": expected_sha, "release_only": release_only}
    if release_only:
        return {
            "status": "pass" if all(checks.values()) else "blocked",
            "blocker_class": None if all(checks.values()) else "integrity",
            "checks": checks,
            **details,
        }

    checks["linux"] = platform.system() == "Linux"
    checks["running_as_root"] = os.geteuid() == 0
    current = Path("/opt/inflector/current")
    checks["current_release_symlink"] = (
        current.is_symlink() and current.resolve() == release.resolve()
    )
    required_directories = (
        Path("/srv/inflector/archive/nse"),
        Path("/srv/inflector/archive/gdelt"),
        Path("/srv/inflector/config"),
    )
    checks["persistent_directories"] = all(path.is_dir() for path in required_directories)
    tools = ("git", "node", "npm", "pg_dump", "systemd-analyze", "systemctl", "runuser")
    checks["required_binaries"] = all(shutil.which(tool) is not None for tool in tools)
    try:
        python_version = _run(
            (str(release / ".venv/bin/python"), "--version"), runner=runner
        ).stdout.strip()
        node_version = _run(("node", "--version"), runner=runner).stdout.strip()
        checks["supported_python"] = python_version.startswith("Python 3.12.")
        checks["supported_node"] = (
            node_version.startswith("v") and int(node_version[1:].split(".", 1)[0]) >= 22
        )
    except (DeploymentError, ValueError):
        checks["supported_python"] = False
        checks["supported_node"] = False
    env_values: dict[str, str] = {}
    env_ok = True
    for name, path in ENV_FILES.items():
        valid, values = _environment_file_check(path)
        checks[f"{name}_environment_file"] = valid
        env_ok = env_ok and valid
        env_values.update(values)
    required_cycle = (
        "INFLECTOR_PRODUCTION_DATABASE_URL",
        "INFLECTOR_PRODUCTION_RAW_ROOT",
        "INFLECTOR_GDELT_RAW_ROOT",
        "INFLECTOR_NSE_LICENSE_CLASS",
        "INFLECTOR_GDELT_LICENSE_CLASS",
        "INFLECTOR_MODEL_FAMILY",
        "INFLECTOR_RESEARCH_PROFILE",
        "INFLECTOR_OPERATIONS_PROFILE",
        "INFLECTOR_SYMBOLS_FILE",
        "INFLECTOR_MODEL_SEMANTIC_VERSION",
        "INFLECTOR_DEPLOYED_GIT_SHA",
        "INFLECTOR_MODEL_EFFECTIVE_FROM",
    )
    checks["required_environment_values_present"] = env_ok and all(
        env_values.get(name, "").strip() for name in required_cycle
    )
    checks["deployed_sha_environment_matches"] = (
        env_values.get("INFLECTOR_DEPLOYED_GIT_SHA") == expected_sha
    )
    checks["accepted_environment_bindings"] = (
        env_values.get("INFLECTOR_PRODUCTION_RAW_ROOT") == "/srv/inflector/archive/nse"
        and env_values.get("INFLECTOR_GDELT_RAW_ROOT") == "/srv/inflector/archive/gdelt"
        and env_values.get("INFLECTOR_MODEL_FAMILY") == "inflector_v1"
        and env_values.get("INFLECTOR_RESEARCH_PROFILE")
        == "config/research/production_research_v4.json"
        and env_values.get("INFLECTOR_OPERATIONS_PROFILE")
        == "config/operations/production_operations_v6.json"
    )
    symbols_value = env_values.get("INFLECTOR_SYMBOLS_FILE", "")
    checks["symbols_file_present"] = bool(symbols_value) and Path(symbols_value).is_file()
    checks["api_runtime_configuration_present"] = bool(
        env_values.get("DATABASE_URL", "").strip()
        and env_values.get("CORS_ORIGINS", "").strip()
    )
    checks["database_urls_match"] = (
        env_values.get("DATABASE_URL")
        == env_values.get("INFLECTOR_PRODUCTION_DATABASE_URL")
    )
    checks["web_runtime_is_production"] = env_values.get("NODE_ENV") == "production"
    checks["caddy_hosts_present"] = bool(
        env_values.get("INFLECTOR_PUBLIC_API_HOST", "").strip()
        and env_values.get("INFLECTOR_PUBLIC_WEB_HOST", "").strip()
    )
    checks["web_receives_no_secrets"] = (
        not any(
            key in _parse_environment_file(ENV_FILES["web"])
            for key in (
                "DATABASE_URL",
                "INFLECTOR_PRODUCTION_DATABASE_URL",
                "INFLECTOR_TELEGRAM_BOT_TOKEN",
            )
        )
        if ENV_FILES["web"].is_file()
        else False
    )
    checks["api_receives_no_telegram"] = (
        not any(
            key in _parse_environment_file(ENV_FILES["api"])
            for key in ("INFLECTOR_TELEGRAM_BOT_TOKEN", "INFLECTOR_TELEGRAM_CHAT_ID")
        )
        if ENV_FILES["api"].is_file()
        else False
    )
    for directory in required_directories[:2]:
        try:
            _run(("runuser", "-u", "inflector", "--", "test", "-w", str(directory)), runner=runner)
            writable = True
        except DeploymentError:
            writable = False
        checks[f"{directory.name}_writable_by_cycle"] = writable
    installed_units = [Path("/etc/systemd/system") / name for name in UNIT_NAMES]
    checks["systemd_units_installed"] = all(path.is_file() for path in installed_units)
    if checks["systemd_units_installed"]:
        try:
            _run(
                ("systemd-analyze", "verify", *(str(path) for path in installed_units)),
                runner=runner,
            )
            checks["systemd_units_valid"] = True
        except DeploymentError:
            checks["systemd_units_valid"] = False
    else:
        checks["systemd_units_valid"] = False
    try:
        timer_state = runner(
            ("systemctl", "is-enabled", "inflector-cycle.timer"),
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        checks["recurring_timer_disabled"] = timer_state in {
            "disabled",
            "static",
            "not-found",
        }
    except OSError:
        checks["recurring_timer_disabled"] = False
    if shutil.which("caddy") is not None:
        try:
            caddy_environment = dict(os.environ)
            caddy_environment.update(_parse_environment_file(ENV_FILES["caddy"]))
            _run(
                ("caddy", "validate", "--config", "/etc/caddy/Caddyfile"),
                environment=caddy_environment,
                runner=runner,
            )
            checks["caddy_configuration_valid"] = True
        except DeploymentError:
            checks["caddy_configuration_valid"] = False
    else:
        checks["caddy_configuration_valid"] = False
    for service in ("inflector-api.service", "inflector-web.service", "caddy.service"):
        try:
            active = runner(
                ("systemctl", "is-active", service),
                check=False,
                capture_output=True,
                text=True,
            )
            checks[f"{service}_active"] = (
                active.returncode == 0 and active.stdout.strip() == "active"
            )
        except OSError:
            checks[f"{service}_active"] = False
    for name, url in (
        ("api_loopback_health", "http://127.0.0.1:8000/health"),
        ("web_loopback_health", "http://127.0.0.1:3000/"),
    ):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                checks[name] = response.status == 200
        except (OSError, urllib.error.URLError):
            checks[name] = False
    if checks["required_environment_values_present"]:
        runtime = _runtime_readiness(release, env_values)
        details["runtime"] = runtime
        checks["operations_health_ready"] = runtime["operations_health_status"] == "ready"
        checks["release_readiness_ready"] = runtime["release_readiness_status"] == "ready"
        checks["active_subject_champion"] = runtime["active_subject"] == "champion"
    else:
        checks["operations_health_ready"] = False
        checks["release_readiness_ready"] = False
        checks["active_subject_champion"] = False
    status = "pass" if all(checks.values()) else "blocked"
    integrity_names = {
        "release_tree",
        "release_policy_checksum",
        "deployed_sha_environment_matches",
    }
    integrity_failed = any(not checks.get(name, False) for name in integrity_names)
    return {
        "status": status,
        "blocker_class": None
        if status == "pass"
        else ("integrity" if integrity_failed else "environment"),
        "checks": checks,
        **details,
    }


def install_systemd_units(
    *,
    release: Path,
    enable_api: bool,
    enable_web: bool,
    runner: Runner = subprocess.run,
    destination: Path = Path("/etc/systemd/system"),
    require_root: bool = True,
) -> None:
    if require_root:
        _require_linux_root()
    source = release / "deploy/systemd"
    units = [source / name for name in UNIT_NAMES]
    if not all(path.is_file() for path in units):
        raise DeploymentError("reviewed release does not contain every required systemd unit")
    _run(("systemd-analyze", "verify", *(str(path) for path in units)), runner=runner)
    destination.mkdir(parents=True, exist_ok=True)
    for unit in units:
        shutil.copyfile(unit, destination / unit.name)
        (destination / unit.name).chmod(0o644)
    caddy_drop_in_source = release / "deploy/caddy" / CADDY_DROP_IN
    if not caddy_drop_in_source.is_file():
        raise DeploymentError("reviewed release does not contain the Caddy environment drop-in")
    caddy_drop_in_destination = destination / "caddy.service.d"
    caddy_drop_in_destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(caddy_drop_in_source, caddy_drop_in_destination / "inflector.conf")
    (caddy_drop_in_destination / "inflector.conf").chmod(0o644)
    installed = [destination / name for name in UNIT_NAMES]
    _run(("systemd-analyze", "verify", *(str(path) for path in installed)), runner=runner)
    _run(("systemctl", "daemon-reload"), runner=runner)
    if enable_api:
        _run(("systemctl", "enable", "--now", "inflector-api.service"), runner=runner)
    if enable_web:
        _run(("systemctl", "enable", "--now", "inflector-web.service"), runner=runner)
    # The recurring timer is intentionally never enabled by this installer.


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap-host")
    install = commands.add_parser("install-release")
    install.add_argument("--sha", required=True)
    install.add_argument("--source-dir", type=Path, required=True)
    systemd = commands.add_parser("install-systemd")
    systemd.add_argument("--release", type=Path, default=Path("/opt/inflector/current"))
    systemd.add_argument("--enable-api", action="store_true")
    systemd.add_argument("--enable-web", action="store_true")
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--expected-sha", required=True)
    preflight.add_argument("--release", type=Path, default=Path("/opt/inflector/current"))
    preflight.add_argument("--release-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bootstrap-host":
            bootstrap_host()
            payload: dict[str, object] = {"status": "ready", "operation": "bootstrap-host"}
        elif args.command == "install-release":
            target = install_release(
                source=args.source_dir.resolve(),
                requested_sha=args.sha,
                public_api_base_url=os.getenv("NEXT_PUBLIC_API_BASE_URL", ""),
            )
            payload = {"status": "ready", "operation": "install-release", "release": str(target)}
        elif args.command == "install-systemd":
            install_systemd_units(
                release=args.release.resolve(),
                enable_api=args.enable_api,
                enable_web=args.enable_web,
            )
            payload = {
                "status": "ready",
                "operation": "install-systemd",
                "recurring_timer_enabled": False,
            }
        else:
            payload = production_preflight(
                expected_sha=args.expected_sha,
                release=args.release.resolve(),
                release_only=args.release_only,
            )
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload["status"] in {"ready", "pass"} else 1
    except Exception as error:  # noqa: BLE001 - sanitized deployment boundary
        print(
            json.dumps(
                {"status": "failed", "error_code": type(error).__name__, "message": str(error)},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
