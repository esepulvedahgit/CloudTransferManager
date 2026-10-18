from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

_PAGE_SIZE = 200
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    formula_segment = re.search(r"(?:^|[/\\])(?:[ \t\r\n]*[=+\-@]|[\t\r])", text)
    if text.startswith(_FORMULA_PREFIXES) or formula_segment is not None:
        return "'" + text
    return text


def _csv_row(values: list[Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([_safe_cell(value) for value in values])
    return buffer.getvalue()


async def iter_omissions_csv(store: Any, transfer_id: uuid.UUID) -> AsyncIterator[str]:
    """Yield the explicit omission report in bounded, cursor-paged CSV chunks."""
    yield _csv_row(
        ["ruta_origen", "destino_asignado", "motivo", "fecha_omision", "tamano_bytes"]
    )
    after_id: uuid.UUID | None = None
    while True:
        rows = await store.alist_transfer_manifest_items(
            transfer_id,
            statuses={"SKIPPED"},
            after_id=after_id,
            limit=_PAGE_SIZE,
        )
        for item in rows:
            if item.omission_kind is None or item.omitted_at is None:
                continue
            yield _csv_row(
                [
                    item.source_path,
                    item.destination_path,
                    item.omission_kind,
                    item.omitted_at.isoformat(),
                    item.size_bytes,
                ]
            )
        if len(rows) < _PAGE_SIZE:
            break
        after_id = rows[-1].id
