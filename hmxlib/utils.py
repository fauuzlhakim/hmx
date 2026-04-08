import datetime as dt
import sys
from typing import Any

def err(msg: str, exit_code: int = 1) -> None:
    print(f'error: {msg}', file=sys.stderr)
    raise SystemExit(exit_code)

def utcnow() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)


def now_iso() -> str:
    return utcnow().isoformat() + 'Z'


def slugify(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {'@', '.', '-', '_', '+'}:
            out.append('-')
    s = ''.join(out).strip('-')
    while '--' in s:
        s = s.replace('--', '-')
    return s or 'account'


def deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def deep_set(data: dict[str, Any], dotted: str, value: Any) -> None:
    cur = data
    parts = dotted.split('.')
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value

def parse_iso8601_utc(value: Any) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


FIVE_HOURS_SECONDS = 5 * 3600


def format_duration_compact(seconds: int | float | None) -> str:
    if seconds is None:
        return '-'
    remaining = max(0, int(seconds))
    hours, rem = divmod(remaining, 3600)
    minutes, _ = divmod(rem, 60)
    if hours and minutes:
        return f'{hours}h{minutes:02}m'
    if hours:
        return f'{hours}h'
    return f'{minutes}m'


def format_percent_compact(value: float | None) -> str:
    if value is None:
        return '-'
    pct = max(0.0, min(100.0, float(value)))
    return f'{int(round(pct))}%'


def five_hour_window_remaining(limit: dict[str, Any], now: dt.datetime) -> str:
    state = str(limit.get('state') or '').strip().lower()
    if state != 'limited':
        return '-'

    reset_at = parse_iso8601_utc(limit.get('reset_at'))
    if reset_at is None or reset_at <= now:
        return '-'

    seconds_left = limit.get('resets_in_seconds')
    if not isinstance(seconds_left, (int, float)) or seconds_left <= 0:
        seconds_left = int((reset_at - now).total_seconds())

    if seconds_left <= 0 or seconds_left > FIVE_HOURS_SECONDS:
        return '-'

    return format_percent_compact((seconds_left / FIVE_HOURS_SECONDS) * 100)

