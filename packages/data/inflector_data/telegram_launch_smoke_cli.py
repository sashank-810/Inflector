"""Operator-only CLI for one reviewed Production O Telegram launch smoke."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from inflector_data.model_release_policy import load_model_release_policy
from inflector_data.notification_delivery_operations import (
    load_bound_notification_delivery_policy,
)
from inflector_data.operations_profile import load_operations_profile
from inflector_data.telegram_launch_smoke import run_operator_telegram_smoke


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send exactly one explicitly reviewed Production O delivery"
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--delivery-id", type=UUID, required=True)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    return parser


def _execute(args: argparse.Namespace, session: Session) -> dict[str, object]:
    root = args.repository_root.resolve()
    profile = load_operations_profile(
        root / "config/operations/production_operations_v6.json"
    )
    delivery_policy = load_bound_notification_delivery_policy(
        profile,
        repository_root=root,
    )
    release_policy = load_model_release_policy(
        root / "config/operations/production_model_release_v1.json"
    )
    return run_operator_telegram_smoke(
        session,
        delivery_id=args.delivery_id,
        policy=delivery_policy,
        release_policy=release_policy,
    ).as_dict()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine: Engine | None = None
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        factory = sessionmaker(bind=engine, autoflush=False)
        with factory() as session:
            payload = _execute(args, session)
        print(json.dumps(payload, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - sanitized operator boundary
        print(
            json.dumps(
                {"status": "failed", "error": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
