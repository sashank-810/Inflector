"""Enumerate explicit ORM/Alembic identifier names and enforce PostgreSQL's limit."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from inflector_database.base import Base

POSTGRES_IDENTIFIER_MAX_BYTES = 63
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Identifier:
    name: str
    origin: str


def explicit_identifiers(root: Path = ROOT) -> tuple[Identifier, ...]:
    """Return explicit current ORM and migration constraint/index names."""

    found: set[tuple[str, str]] = set()
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            if index.name:
                found.add((index.name, f"orm:{table.name}:index"))
        for constraint in table.constraints:
            if constraint.name:
                found.add((constraint.name, f"orm:{table.name}:constraint"))
    for path in sorted((root / "migrations" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = _call_name(node.func)
            name = _explicit_name(call, node)
            if name is not None:
                found.add((name, f"alembic:{path.name}:{node.lineno}"))
    return tuple(Identifier(name, origin) for name, origin in sorted(found))


def audit(root: Path = ROOT) -> dict[str, object]:
    identifiers = explicit_identifiers(root)
    violations = tuple(
        {"name": item.name, "bytes": len(item.name.encode("utf-8")), "origin": item.origin}
        for item in identifiers
        if len(item.name.encode("utf-8")) > POSTGRES_IDENTIFIER_MAX_BYTES
    )
    maximum = max((len(item.name.encode("utf-8")) for item in identifiers), default=0)
    return {
        "status": "pass" if not violations else "fail",
        "identifier_count": len(identifiers),
        "maximum_identifier_bytes": maximum,
        "limit_bytes": POSTGRES_IDENTIFIER_MAX_BYTES,
        "violations": violations,
    }


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _explicit_name(call: str, node: ast.Call) -> str | None:
    positional_name_calls = {
        "Index",
        "sa.Index",
        "op.create_index",
        "op.drop_index",
        "op.create_unique_constraint",
        "op.drop_constraint",
        "op.create_check_constraint",
        "op.create_foreign_key",
        "op.create_primary_key",
    }
    constraint_calls = {
        "CheckConstraint",
        "ForeignKeyConstraint",
        "PrimaryKeyConstraint",
        "UniqueConstraint",
        "sa.CheckConstraint",
        "sa.ForeignKeyConstraint",
        "sa.PrimaryKeyConstraint",
        "sa.UniqueConstraint",
    }
    if call in positional_name_calls and node.args:
        value = _string_literal(node.args[0])
        if value is not None:
            return value
    if call in constraint_calls:
        for keyword in node.keywords:
            if keyword.arg == "name":
                return _string_literal(keyword.value)
    return None


def _string_literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def main() -> int:
    result = audit()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
