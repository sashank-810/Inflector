"""Guard committed CSV fixtures against positional-field drift."""

import csv
from pathlib import Path

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures"


def test_committed_csv_fixture_rows_match_their_header_shape() -> None:
    """Every non-empty fixture row must retain the exact declared field count."""

    for path in sorted(FIXTURE_DIRECTORY.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert rows, f"{path.name} must contain a header"
        width = len(rows[0])
        for line_number, row in enumerate(rows[1:], start=2):
            assert len(row) == width, (
                f"{path.name}:{line_number} has {len(row)} fields; expected {width}"
            )
