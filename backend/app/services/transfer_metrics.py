from __future__ import annotations

import math
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def memory_dashboard(
    store: Any,
    *,
    requested_by: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
    timezone: str,
    source_provider: str | None = None,
    destination_provider: str | None = None,
    source_connection_id: uuid.UUID | None = None,
    destination_connection_id: uuid.UUID | None = None,
    include_mock: bool = False,
) -> dict[str, Any]:
    zone = ZoneInfo(timezone)
    connections = store.connections

    def matches_filters(job: Any) -> bool:
        return (
            (requested_by is None or job.requested_by == requested_by)
            and job.source_connection_id in connections
            and job.destination_connection_id in connections
            and (
                include_mock
                or (
                    connections[job.source_connection_id].provider_type != "MOCK"
                    and connections[job.destination_connection_id].provider_type != "MOCK"
                )
            )
            and (
                source_provider is None
                or connections[job.source_connection_id].provider_type == source_provider
            )
            and (
                destination_provider is None
                or connections[job.destination_connection_id].provider_type == destination_provider
            )
            and (source_connection_id is None or job.source_connection_id == source_connection_id)
            and (
                destination_connection_id is None
                or job.destination_connection_id == destination_connection_id
            )
        )

    all_filtered_jobs = [job for job in store.transfers.values() if matches_filters(job)]
    jobs = [
        job
        for job in all_filtered_jobs
        if job.created_at is not None and date_from <= job.created_at < date_to
    ]
    job_ids = {job.id for job in jobs}
    activity_job_ids = {job.id for job in all_filtered_jobs}
    selected_items = [
        item
        for (transfer_id, _digest), item in store.transfer_manifest_items.items()
        if transfer_id in job_ids and not item.is_dir
    ]
    grouped_items: dict[uuid.UUID, list[Any]] = defaultdict(list)
    manifest_transfer_ids = {
        transfer_id
        for transfer_id, _digest in store.transfer_manifest_items
        if transfer_id in job_ids
    }
    for item in selected_items:
        grouped_items[item.transfer_id].append(item)

    statuses = Counter(job.status for job in jobs)
    copied = [item for item in selected_items if item.status == "COPIED"]
    failed = [item for item in selected_items if item.status == "FAILED"]
    size_buckets: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    for item in copied:
        size = item.size_bytes
        if size is None:
            size_buckets["unknown"] += 1
        elif size == 0:
            size_buckets["zero"] += 1
        elif size < 1024**2:
            size_buckets["under_1_mib"] += 1
        elif size < 100 * 1024**2:
            size_buckets["1_to_100_mib"] += 1
        elif size < 1024**3:
            size_buckets["100_mib_to_1_gib"] += 1
        else:
            size_buckets["over_1_gib"] += 1
        suffix = PurePosixPath(item.source_path).suffix.casefold().lstrip(".") or "Sin extensión"
        extensions[suffix] += 1

    def file_summary(items: list[Any]) -> dict[str, Any]:
        return {
            "copied": sum(item.status == "COPIED" for item in items),
            "copied_bytes": sum(item.size_bytes or 0 for item in items if item.status == "COPIED"),
            "copied_size_unknown": sum(
                item.status == "COPIED" and item.size_bytes is None for item in items
            ),
            "failed": sum(item.status == "FAILED" for item in items),
            "skipped_historical": sum(
                item.status == "SKIPPED" and not item.omission_kind for item in items
            ),
            "omitted": sum(
                item.status == "SKIPPED" and item.omission_kind is not None for item in items
            ),
            "omitted_bytes": sum(
                item.size_bytes or 0
                for item in items
                if item.status == "SKIPPED" and item.omission_kind is not None
            ),
            "pending": sum(item.status == "PENDING" for item in items),
            "renamed": sum(item.rename_reason is not None for item in items),
            "total": len(items),
        }

    source_providers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"transfers": 0, "files": file_summary([])}
    )
    destination_providers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"transfers": 0, "files": file_summary([])}
    )
    pairs: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"transfers": 0, "files": file_summary([])}
    )
    for job in jobs:
        source = connections[job.source_connection_id]
        destination = connections[job.destination_connection_id]
        entries = grouped_items[job.id]
        for target, key in (
            (source_providers, source.provider_type),
            (destination_providers, destination.provider_type),
            (pairs, f"{source.provider_type} → {destination.provider_type}"),
        ):
            target[key]["transfers"] += 1
            current = target[key]["files"]
            addition = file_summary(entries)
            for field in current:
                current[field] += addition[field]

    by_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {"transfers_created": 0, "copied": 0, "copied_bytes": 0}
    )
    for job in jobs:
        day = job.created_at.astimezone(zone).date().isoformat()
        by_day[day]["transfers_created"] += 1
    copied_in_activity_range = [
        item
        for (transfer_id, _digest), item in store.transfer_manifest_items.items()
        if transfer_id in activity_job_ids
        and not item.is_dir
        and item.status == "COPIED"
        and item.copied_at is not None
    ]
    for item in copied_in_activity_range:
        occurred = item.copied_at
        if occurred is not None and date_from <= occurred < date_to:
            day = occurred.astimezone(zone).date().isoformat()
            by_day[day]["copied"] += 1
            by_day[day]["copied_bytes"] += item.size_bytes or 0

    state_spans = [
        span
        for span in store.metric_spans
        if span["transfer_job_id"] in activity_job_ids
        and span["span_type"] == "STATE"
        and span["started_at"] < date_to
        and (span["completed_at"] is None or span["completed_at"] >= date_from)
    ]
    stage_spans = [
        span
        for span in store.metric_spans
        if span["transfer_job_id"] in activity_job_ids
        and span["span_type"] == "STAGE"
        and date_from <= span["started_at"] < date_to
    ]
    state_groups: dict[str, list[int]] = defaultdict(list)
    for span in state_spans:
        if span["duration_ms"] is not None and span["outcome"] == "SUCCESS":
            state_groups[span["code"]].append(span["duration_ms"])
    stage_groups: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for span in stage_spans:
        stage_groups[(span["code"], span["provider_type"], span["connection_role"])].append(span)

    performance = []
    for (code, provider_type, role), samples in sorted(stage_groups.items()):
        successful = [
            sample
            for sample in samples
            if sample["outcome"] == "SUCCESS" and sample["duration_ms"] is not None
        ]
        durations = [sample["duration_ms"] for sample in successful]
        total_bytes = sum(sample["bytes_count"] or 0 for sample in successful)
        elapsed = sum(durations)
        performance.append(
            {
                "stage": code,
                "provider_type": provider_type,
                "connection_role": role,
                "samples": len(samples),
                "successful_samples": len(successful),
                "failed_samples": sum(sample["outcome"] == "FAILED" for sample in samples),
                "incomplete_samples": sum(sample["outcome"] == "OPEN" for sample in samples),
                "median_ms": percentile(durations, 0.5),
                "p95_ms": percentile(durations, 0.95),
                "bytes_count": total_bytes,
                "effective_bytes_per_second": round(total_bytes * 1000 / elapsed)
                if elapsed
                else None,
            }
        )

    active_jobs = [
        job
        for job in all_filtered_jobs
        if job.status in {"VALIDATING", "RUNNING", "RETRYING", "PAUSING", "PAUSED", "CANCELLING"}
        and (requested_by is None or job.requested_by == requested_by)
    ]
    system = (
        store.dashboard_system_summary() if hasattr(store, "dashboard_system_summary") else None
    )
    return {
        "summary": {
            "transfers": {
                "total": len(jobs),
                "by_status": dict(statuses),
                "completed_cleanly": statuses["COMPLETED"],
                "completed_with_warnings": statuses["COMPLETED_WITH_WARNINGS"],
                "failed": statuses["FAILED"],
                "cancelled": statuses["CANCELLED"],
                "success_rate": round(
                    (statuses["COMPLETED"] + statuses["COMPLETED_WITH_WARNINGS"])
                    * 100
                    / max(
                        statuses["COMPLETED"]
                        + statuses["COMPLETED_WITH_WARNINGS"]
                        + statuses["FAILED"],
                        1,
                    ),
                    1,
                ),
                "additional_attempts": sum(max(0, job.attempt_count - 1) for job in jobs),
            },
            "files": file_summary(selected_items),
        },
        "providers": {
            "source": [
                {"provider_type": key, **value} for key, value in sorted(source_providers.items())
            ],
            "destination": [
                {"provider_type": key, **value}
                for key, value in sorted(destination_providers.items())
            ],
            "pairs": [{"label": key, **value} for key, value in sorted(pairs.items())],
        },
        "activity": [{"date": key, **value} for key, value in sorted(by_day.items())],
        "performance": {
            "stages": performance,
            "states": [
                {
                    "state": key,
                    "samples": len(values),
                    "median_ms": percentile(values, 0.5),
                    "p95_ms": percentile(values, 0.95),
                }
                for key, values in sorted(state_groups.items())
            ],
        },
        "reliability": {
            "errors": [
                {"error_code": key or "UNKNOWN", "files": count}
                for key, count in Counter(item.error_code for item in failed).most_common(10)
            ],
            "size_distribution": [
                {"bucket": key, "files": size_buckets[key]}
                for key in (
                    "zero",
                    "under_1_mib",
                    "1_to_100_mib",
                    "100_mib_to_1_gib",
                    "over_1_gib",
                    "unknown",
                )
            ],
            "extensions": [
                {"extension": key, "files": count} for key, count in extensions.most_common(6)
            ],
        },
        "scheduling": {
            "waiting": sum(job.status in {"SCHEDULED", "QUEUED"} for job in jobs),
            "paused": sum(job.status in {"PAUSING", "PAUSED"} for job in jobs),
            "by_wait_reason": [
                {"reason": key or "OTHER", "transfers": count}
                for key, count in Counter(
                    job.wait_reason for job in jobs if job.status in {"SCHEDULED", "QUEUED"}
                ).items()
            ],
        },
        "live": {
            "active": len(active_jobs),
            "by_status": dict(Counter(job.status for job in active_jobs)),
            "reported_speed_bps": sum(job.current_speed_bps or 0 for job in active_jobs),
        },
        "coverage": {
            "transfers": len(jobs),
            "with_manifest": sum(job.id in manifest_transfer_ids for job in jobs),
            "manifestless_transfers": sum(job.id not in manifest_transfer_ids for job in jobs),
            "copied_files_with_date": sum(
                item.status == "COPIED" and item.copied_at is not None for item in selected_items
            ),
            "copied_files_without_date": sum(
                item.status == "COPIED" and item.copied_at is None for item in selected_items
            ),
            "stage_samples": len(stage_spans),
            "stage_samples_incomplete": sum(span["outcome"] == "OPEN" for span in stage_spans),
        },
        "system": system,
    }
