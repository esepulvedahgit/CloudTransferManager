import json
import re
from typing import Any

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtpe]?(?:i?b|i?bytes?))\s*$", re.I)
_ETA = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$", re.I)
_MEGA_PROGRESS = re.compile(
    r"^\s*TRANSFER{1,2}ING\s+\|+[^()\r\n]*\|+\s*"
    r"\(\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)"
    r"\s*:\s*(\d+(?:\.\d+)?)\s*%\s*\)\s*$",
    re.IGNORECASE,
)


def _byte_count(value: str) -> int | None:
    match = _SIZE.fullmatch(value)
    if match is None:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    prefix = unit[0] if unit[0] in "kmgtpe" else ""
    power = "" if not prefix else ("i" if "i" in unit else "d")
    exponent = "kmgtpe".index(prefix) + 1 if prefix else 0
    base = 1024 if power == "i" or "byte" in unit else 1000
    return int(round(amount * base**exponent))


def _duration_seconds(value: str) -> int | None:
    cleaned = value.strip().lower()
    if cleaned in {"", "-", "unknown"}:
        return None
    match = _ETA.fullmatch(cleaned)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return round(int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0))


def parse_rclone_stats_line(line: str) -> dict[str, Any] | None:
    """Parse one Rclone NDJSON log line, returning only safe numeric telemetry."""
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("msg"), str):
        return None

    messages = entry["msg"]
    lines = [part.strip() for part in messages.splitlines()]
    result: dict[str, Any] = {}
    for part in lines:
        item_match = re.match(r"^\*\s+(.+?):\s*\d+(?:\.\d+)?%", part)
        if item_match:
            result["current_item"] = item_match.group(1)[:1024]
            break
    transferred = [
        part.removeprefix("Transferred:").strip()
        for part in lines
        if part.startswith("Transferred:")
    ]
    byte_match = (
        re.match(
            r"^([^/]+?)\s*/\s*([^,]+)(?:,\s*([\d.]+)%)?(?:,\s*([^,]+))?(?:,\s*ETA\s+(.+))?$",
            transferred[0],
        )
        if transferred
        else None
    )
    if byte_match:
        bytes_done = _byte_count(byte_match.group(1))
        bytes_total = _byte_count(byte_match.group(2))
        if bytes_done is not None:
            result["bytes_transferred"] = bytes_done
        if bytes_total is not None:
            result["bytes_total"] = bytes_total
        if byte_match.group(3):
            result["progress_percent"] = float(byte_match.group(3))
        speed_text = (byte_match.group(4) or "").strip()
        if speed_text.lower().endswith("/s"):
            speed_text = speed_text[:-2].strip()
        speed = _byte_count(speed_text)
        if speed is not None:
            result["current_speed_bps"] = speed
        if byte_match.group(5):
            eta = _duration_seconds(byte_match.group(5))
            if eta is not None:
                result["estimated_remaining_seconds"] = eta

    count_candidates = transferred[1:] + [
        part.removeprefix("Checks:").strip() for part in lines if part.startswith("Checks:")
    ]
    for candidate in count_candidates:
        match = re.match(r"^(\d+)\s*/\s*(\d+)(?:,\s*([\d.]+)%)?$", candidate)
        if match:
            result["files_completed"] = int(match.group(1))
            result["files_total"] = int(match.group(2))
            if "progress_percent" not in result and match.group(3):
                result["progress_percent"] = float(match.group(3))
            break

    if not result:
        return None
    if "progress_percent" not in result:
        done = result.get("bytes_transferred")
        total = result.get("bytes_total")
        if isinstance(done, int) and isinstance(total, int) and total > 0:
            result["progress_percent"] = round(done * 100 / total, 2)
    return result


def parse_mega_progress_line(line: str) -> dict[str, Any] | None:
    """Parse MEGAcmd's numeric progress bar, ignoring paths and other output."""
    match = _MEGA_PROGRESS.fullmatch(line.strip())
    if match is None:
        return None
    completed, total, unit, percent = match.groups()
    bytes_completed = _byte_count(f"{completed} {unit}")
    bytes_total = _byte_count(f"{total} {unit}")
    if bytes_completed is None or bytes_total is None:
        return None
    return {
        "bytes_transferred": bytes_completed,
        "bytes_total": bytes_total,
        "progress_percent": float(percent),
    }
