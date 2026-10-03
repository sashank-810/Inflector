"""Deterministic plain-text rendering for Production O Telegram delivery."""

from __future__ import annotations

from inflector_database.models import ResearchNotificationOutbox

RENDERING_VERSION = "research_notification_telegram_v1"
TELEGRAM_SAFE_MESSAGE_LIMIT = 4000

TRIGGER_LABELS = {
    "entered_compared_universe": "Entered the compared research universe",
    "left_compared_universe": "Left the compared research universe",
    "symbol_changed": "Security symbol changed",
    "became_rankable": "Became rankable",
    "lost_rankability": "Lost rankability",
    "score_increased": "Research score increased",
    "score_decreased": "Research score decreased",
    "rank_moved_up": "Research rank moved up",
    "rank_moved_down": "Research rank moved down",
    "coverage_completed": "Research coverage completed",
    "coverage_regressed": "Research coverage regressed",
    "components_gained": "Research components gained",
    "components_lost": "Research components lost",
    "became_eligible": "Became eligible under V5",
    "became_ineligible": "Became ineligible under V5",
    "became_stale": "Research became stale",
    "recovered_from_stale": "Research recovered from stale state",
    "unranked_reason_changed": "Unranked reason changed",
}


def render_telegram_notification(
    outbox: ResearchNotificationOutbox,
    *,
    delivery_key_sha256: str,
    rendering_version: str,
) -> str:
    """Render one immutable N event without consulting live research state."""

    if rendering_version != RENDERING_VERSION:
        raise ValueError("unsupported Telegram notification rendering version")
    matched = tuple(_trigger_codes(outbox.matched_trigger_codes_json))
    if not matched:
        raise ValueError("notification has no matched trigger codes")
    try:
        labels = tuple(TRIGGER_LABELS[code] for code in matched)
    except KeyError as error:
        raise ValueError("notification contains an unsupported trigger code") from error
    payload = outbox.payload_json
    symbol = outbox.current_symbol or outbox.baseline_symbol
    if symbol is None:
        symbol = f"identity {outbox.notification_key_sha256[:12]}"
    lines = [f"Inflector research update — {symbol}", ""]
    lines.extend(f"• {label}" for label in labels)
    current_score = _optional_text(payload, "current_final_score")
    current_rank = _optional_integer(payload, "current_score_rank")
    if current_score is not None or current_rank is not None:
        lines.append("")
    if current_score is not None:
        lines.append(f"Score: {current_score}")
    if current_rank is not None:
        lines.append(f"Rank: {current_rank}")
    lines.extend(("", f"Event: {delivery_key_sha256[:12]}"))
    rendered = "\n".join(lines)
    if len(rendered) > TELEGRAM_SAFE_MESSAGE_LIMIT:
        raise ValueError("rendered Telegram notification exceeds the safe message limit")
    return rendered


def _trigger_codes(values: list[object]) -> tuple[str, ...]:
    result = tuple(value for value in values if isinstance(value, str))
    if len(result) != len(values) or len(result) != len(set(result)):
        raise ValueError("notification trigger codes are malformed")
    return result


def _optional_text(payload: dict[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"notification payload {field} is malformed")
    return value


def _optional_integer(payload: dict[str, object], field: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"notification payload {field} is malformed")
    return value
