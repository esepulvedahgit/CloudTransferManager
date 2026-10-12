import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from celery import Celery
from redis.asyncio import Redis

from app.services.database_store import DatabaseStore
from app.services.event_bus import RedisEventBus
from app.services.notifications import (
    NotificationDeliveryError,
    enqueue_transfer_notification,
    send_transfer_email,
)
from app.services.smtp_config import load_smtp_server_config
from app.services.transfer_executor import TransferExecutor
from app.settings import get_settings

settings = get_settings()
celery_app = Celery("cloud_transfer_manager", broker=settings.redis_url)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
    task_default_queue="transfers",
    task_routes={
        "app.worker.run_transfer": {"queue": "transfers"},
        "app.worker.send_transfer_notification": {"queue": "transfers"},
    },
)


def _event_payload(record: Any) -> dict[str, Any]:
    data = cast(dict[str, Any], record.public_dict())
    data.update(
        {
            "job_id": str(record.id),
            "current_speed_bps": record.current_speed_bps,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    return data


async def _execute_transfer(transfer_id: uuid.UUID) -> None:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    redis_client = Redis.from_url(current_settings.redis_url, decode_responses=True)
    event_bus = RedisEventBus(redis_client)

    async def publish(record: Any) -> None:
        await store.asave_transfer(record)
        await store.aadd_transfer_event(
            record.id,
            level="ERROR" if record.status == "FAILED" else "INFO",
            event_type=record.status,
            message=f"Estado de transferencia: {record.status}",
            technical_details={
                "progress_percent": record.progress_percent,
                "files_completed": record.files_completed,
                "files_total": record.files_total,
                "bytes_transferred": record.bytes_transferred,
                "bytes_total": record.bytes_total,
                "average_speed_bps": record.average_speed_bps,
                "current_speed_bps": record.current_speed_bps,
                "attempt_count": record.attempt_count,
                "error_count": record.error_count,
            },
            provider_error_code="TRANSFER_FAILED" if record.status == "FAILED" else None,
        )
        await event_bus.publish_progress(str(record.id), _event_payload(record))

    async def should_cancel() -> bool:
        return await event_bus.cancellation_requested(str(transfer_id))

    try:
        await TransferExecutor(current_settings, store, publish, should_cancel).execute(transfer_id)
        await enqueue_transfer_notification(store, transfer_id, send_transfer_notification)
    finally:
        await event_bus.clear_cancel(str(transfer_id))
        await event_bus.aclose()
        await store.aclose()


@celery_app.task(name="app.worker.run_transfer", bind=True)  # type: ignore[untyped-decorator]
def run_transfer(self: Any, transfer_id: str) -> None:
    del self
    asyncio.run(_execute_transfer(uuid.UUID(transfer_id)))


async def _send_notification(transfer_id: uuid.UUID) -> None:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    try:
        transfer = await store.aget_transfer(transfer_id)
        if transfer is None or not transfer.send_email_notification:
            return
        user = await store.aget_user(transfer.requested_by)
        source = await store.aget_connection(transfer.source_connection_id)
        destination = await store.aget_connection(transfer.destination_connection_id)
        if user is None or source is None or destination is None:
            return
        smtp = await load_smtp_server_config(store, current_settings)
        send_transfer_email(
            current_settings,
            smtp,
            user.email,
            transfer,
            source.name,
            destination.name,
        )
    finally:
        await store.aclose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.worker.send_transfer_notification",
    bind=True,
    autoretry_for=(NotificationDeliveryError, ValueError, OSError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def send_transfer_notification(self: Any, transfer_id: str) -> None:
    del self
    asyncio.run(_send_notification(uuid.UUID(transfer_id)))
