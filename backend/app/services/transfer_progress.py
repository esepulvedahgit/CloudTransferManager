from __future__ import annotations


def smb_manifest_progress_percent(
    *,
    bytes_transferred: int,
    bytes_total: int,
    files_total: int,
    counts: dict[str, int],
) -> float:
    if bytes_total > 0:
        percent = bytes_transferred * 100 / bytes_total
    elif files_total > 0:
        percent = counts.get("copied", 0) * 100 / files_total
    else:
        percent = 0.0

    incomplete = any(
        counts.get(status, 0) for status in ("pending", "failed", "skipped", "omitted")
    )
    return min(round(percent, 2), 99.99 if incomplete else 100.0)
