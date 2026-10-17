import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from celery import Celery
from redis.asyncio import Redis

from app.services.database_store import DatabaseStore
from app.services.event_bus import RedisEventBus
from app.services.notifications import (
    NotificationDeliveryError,
    send_transfer_email,
)
from app.services.smtp_config import load_smtp_server_config
from app.services.staging import (
    cleanup_orphaned_transfer_staging_areas as cleanup_staging_areas,
)
from app.services.transfer_evaluation import EvaluationClaim
from app.services.transfer_evaluator import TransferEvaluator
from app.services.transfer_executor import TransferExecutor
from app.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
TRANSFER_VISIBILITY_TIMEOUT_SECONDS = 24 * 60 * 60
celery_app = Celery("cloud_transfer_manager", broker=settings.redis_url)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": TRANSFER_VISIBILITY_TIMEOUT_SECONDS},
    task_ignore_result=True,
    task_default_queue="transfers",
    task_routes={
        "app.worker.run_transfer": {"queue": "transfers"},
        "app.worker.send_transfer_notification": {"queue": "notifications"},
        "app.worker.send_legacy_transfer_notification": {"queue": "notifications"},
        "app.worker.dispatch_pending_transfers": {"queue": "control"},
        "app.worker.dispatch_pending_evaluations": {"queue": "control"},
        "app.worker.run_transfer_evaluation": {"queue": "evaluations"},
        "app.worker.dispatch_pending_notifications": {"queue": "control"},
        "app.worker.cleanup_orphaned_staging_areas": {"queue": "transfers"},
        "app.worker.record_beat_heartbeat": {"queue": "control"},
    },
    beat_schedule={
        "beat-health-heartbeat": {
            "task": "app.worker.record_beat_heartbeat",
            "schedule": 15.0,
            "options": {"queue": "control"},
        },
        "dispatch-pending-transfers": {
            "task": "app.worker.dispatch_pending_transfers",
            "schedule": 15.0,
            "options": {"queue": "control"},
        },
        "dispatch-pending-evaluations": {
            "task": "app.worker.dispatch_pending_evaluations",
            "schedule": 15.0,
            "options": {"queue": "control"},
        },
        "dispatch-pending-notifications": {
            "task": "app.worker.dispatch_pending_notifications",
            "schedule": 15.0,
            "options": {"queue": "control"},
        },
        "cleanup-orphaned-staging-areas": {
            "task": "app.worker.cleanup_orphaned_staging_areas",
            "schedule": 600.0,
            "options": {"queue": "transfers"},
        },
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


async def _execute_transfer(transfer_id: uuid.UUID, dispatch_token: uuid.UUID) -> None:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    redis_client = Redis.from_url(current_settings.redis_url, decode_responses=True)
    event_bus = RedisEventBus(redis_client)

    async def publish(record: Any) -> None:
        record.manifest_summary = (await store.alist_transfer_manifest_summaries([record.id]))[
            record.id
        ]
        await store.asave_transfer(record)
        try:
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
        except Exception as exc:
            logger.warning(
                "Transfer event persistence failed",
                extra={
                    "transfer_id": str(record.id),
                    "exception_type": type(exc).__name__,
                },
            )
        try:
            await event_bus.publish_progress(str(record.id), _event_payload(record))
        except Exception as exc:
            logger.warning(
                "Transfer progress publication failed",
                extra={
                    "transfer_id": str(record.id),
                    "exception_type": type(exc).__name__,
                },
            )

    async def should_cancel() -> bool:
        return await store.ais_transfer_cancel_requested(transfer_id)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(10)
            if not await store.aheartbeat_transfer_dispatch(
                transfer_id, dispatch_token, datetime.now(UTC)
            ):
                return

    try:
        async with store.transfer_execution_lock(transfer_id) as acquired:
            if not acquired:
                # A resumed attempt can be reserved while the previous worker is
                # still leaving its lock. Release only this unstarted token; the
                # store ignores tokens whose execution has already begun.
                await store.arelease_transfer_dispatch(transfer_id, dispatch_token)
                return
            if not await store.astart_transfer_dispatch(
                transfer_id, dispatch_token, datetime.now(UTC)
            ):
                return
            heartbeat_task = asyncio.create_task(heartbeat())
            execution_task: asyncio.Task[None] | None = None
            try:
                execution_task = asyncio.create_task(
                    TransferExecutor(current_settings, store, publish, should_cancel).execute(
                        transfer_id
                    )
                )
                done, _pending = await asyncio.wait(
                    {heartbeat_task, execution_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution_task not in done:
                    heartbeat_error = (
                        heartbeat_task.exception() if not heartbeat_task.cancelled() else None
                    )
                    execution_task.cancel()
                    await asyncio.gather(execution_task, return_exceptions=True)
                    if heartbeat_error is not None:
                        raise heartbeat_error
                    raise RuntimeError("Transfer execution ownership was lost")
                await execution_task
            except asyncio.CancelledError:
                raise
            except Exception:
                await store.afail_transfer_dispatch(
                    transfer_id,
                    dispatch_token,
                    event_type="WORKER_EXECUTION_ERROR",
                    message=(
                        "La ejecución falló de forma inesperada. Revisa el destino "
                        "antes de reintentar."
                    ),
                )
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                if execution_task is not None and not execution_task.done():
                    execution_task.cancel()
                    await asyncio.gather(execution_task, return_exceptions=True)
                try:
                    await event_bus.clear_cancel(str(transfer_id))
                except Exception:
                    # PostgreSQL is the cancellation source of truth.
                    pass
                await store.afinish_transfer_dispatch(transfer_id, dispatch_token)
    finally:
        await event_bus.aclose()
        await store.aclose()


@celery_app.task(name="app.worker.run_transfer", bind=True)  # type: ignore[untyped-decorator]
def run_transfer(self: Any, transfer_id: str, dispatch_token: str) -> None:
    del self
    asyncio.run(_execute_transfer(uuid.UUID(transfer_id), uuid.UUID(dispatch_token)))


async def _dispatch_pending_transfers() -> int:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    redis_client = Redis.from_url(current_settings.redis_url, decode_responses=True)
    event_bus = RedisEventBus(redis_client)
    try:
        batch = await store.adispatch_due_transfers(
            datetime.now(UTC),
            max_concurrent=current_settings.transfer_max_concurrent,
            max_nextcloud_concurrent=current_settings.nextcloud_max_concurrent,
        )
        admitted_ids = {transfer.id for transfer in batch.admitted}
        for transfer in batch.changed:
            if transfer.id not in admitted_ids:
                await store.aadd_transfer_event(
                    transfer.id,
                    level="INFO",
                    event_type="SCHEDULER_UPDATE",
                    message=(
                        f"Transferencia pendiente: {transfer.wait_reason}"
                        if transfer.wait_reason
                        else f"Estado de transferencia: {transfer.status}"
                    ),
                    technical_details={"wait_reason": transfer.wait_reason},
                )
            await event_bus.publish_progress(str(transfer.id), _event_payload(transfer))

        for transfer in batch.admitted:
            token = transfer.dispatch_token
            if token is None:
                continue
            try:
                run_transfer.apply_async(args=[str(transfer.id), str(token)], queue="transfers")
            except Exception:
                await store.arelease_transfer_dispatch(transfer.id, token)
        return len(batch.admitted)
    finally:
        await event_bus.aclose()
        await store.aclose()


@celery_app.task(name="app.worker.dispatch_pending_transfers")  # type: ignore[untyped-decorator]
def dispatch_pending_transfers() -> int:
    return asyncio.run(_dispatch_pending_transfers())


async def _execute_transfer_evaluation(
    transfer_id: uuid.UUID,
    generation: int,
    claim_token: uuid.UUID,
) -> None:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    redis_client = Redis.from_url(current_settings.redis_url, decode_responses=True)
    event_bus = RedisEventBus(redis_client)

    async def publish(record: Any) -> None:
        try:
            await event_bus.publish_progress(str(record.id), _event_payload(record))
        except Exception as exc:
            logger.warning(
                "Evaluation progress publication failed",
                extra={
                    "transfer_id": str(record.id),
                    "exception_type": type(exc).__name__,
                },
            )

    try:
        evaluator = TransferEvaluator(current_settings, store, publish)
        await evaluator.execute(
            EvaluationClaim(
                transfer_id=transfer_id,
                generation=generation,
                claim_token=claim_token,
            )
        )
    finally:
        await event_bus.aclose()
        await store.aclose()


@celery_app.task(name="app.worker.run_transfer_evaluation")  # type: ignore[untyped-decorator]
def run_transfer_evaluation(transfer_id: str, generation: int, claim_token: str) -> None:
    asyncio.run(
        _execute_transfer_evaluation(
            uuid.UUID(transfer_id),
            generation,
            uuid.UUID(claim_token),
        )
    )


async def _dispatch_pending_evaluations() -> int:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    now = datetime.now(UTC)
    try:
        claims = await store.aclaim_due_evaluations(now, limit=20)
        for claim in claims:
            try:
                run_transfer_evaluation.apply_async(
                    args=[str(claim.transfer_id), claim.generation, str(claim.claim_token)],
                    queue="evaluations",
                )
            except Exception as exc:
                logger.warning(
                    "Evaluation task publication failed; its reservation will expire",
                    extra={
                        "transfer_id": str(claim.transfer_id),
                        "generation": claim.generation,
                        "exception_type": type(exc).__name__,
                    },
                )
        return len(claims)
    finally:
        await store.aclose()


@celery_app.task(name="app.worker.dispatch_pending_evaluations")  # type: ignore[untyped-decorator]
def dispatch_pending_evaluations() -> int:
    return asyncio.run(_dispatch_pending_evaluations())


async def _record_beat_heartbeat() -> None:
    current_settings = get_settings()
    redis_client = Redis.from_url(current_settings.redis_url, decode_responses=True)
    event_bus = RedisEventBus(redis_client)
    try:
        await event_bus.record_beat_heartbeat()
    finally:
        await event_bus.aclose()


@celery_app.task(name="app.worker.record_beat_heartbeat")  # type: ignore[untyped-decorator]
def record_beat_heartbeat() -> None:
    asyncio.run(_record_beat_heartbeat())


async def _dispatch_pending_notifications() -> int:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    now = datetime.now(UTC)
    try:
        notification_ids = await store.adispatch_pending_notifications(now)
        for notification_id, dispatch_token in notification_ids:
            try:
                send_transfer_notification.apply_async(
                    args=[str(notification_id), str(dispatch_token)], queue="notifications"
                )
            except Exception as exc:
                await store.arelease_notification_dispatch(notification_id, now, dispatch_token)
                logger.warning(
                    "Notification queue publish failed",
                    extra={
                        "notification_id": str(notification_id),
                        "exception_type": type(exc).__name__,
                    },
                )
        return len(notification_ids)
    finally:
        await store.aclose()


@celery_app.task(name="app.worker.dispatch_pending_notifications")  # type: ignore[untyped-decorator]
def dispatch_pending_notifications() -> int:
    return asyncio.run(_dispatch_pending_notifications())


async def _send_notification(notification_id: uuid.UUID, dispatch_token: uuid.UUID) -> None:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    try:
        delivery_claim = await store.aclaim_notification_delivery(
            notification_id, datetime.now(UTC), dispatch_token
        )
        if delivery_claim is None:
            return
        snapshot, claim_token = delivery_claim
        try:
            transfer = SimpleNamespace(
                public_id=snapshot["public_id"],
                status=snapshot["status"],
                bytes_transferred=snapshot["bytes_transferred"],
                created_at=datetime.fromisoformat(snapshot["created_at"]),
                completed_at=datetime.fromisoformat(snapshot["completed_at"]),
            )
            smtp = await load_smtp_server_config(store, current_settings)
            send_transfer_email(
                current_settings,
                smtp,
                snapshot["recipient"],
                transfer,
                snapshot["source_name"],
                snapshot["destination_name"],
            )
        except ValueError:
            await store.afail_notification_delivery(
                notification_id,
                datetime.now(UTC),
                claim_token,
                "SMTP_CONFIGURATION_INVALID",
            )
            logger.warning(
                "Notification delivery configuration failed",
                extra={"notification_id": str(notification_id)},
            )
        except Exception as exc:
            error_code = (
                "SMTP_DELIVERY_FAILED"
                if isinstance(exc, (NotificationDeliveryError, OSError))
                else "SMTP_CONFIGURATION_INVALID"
            )
            await store.afail_notification_delivery(
                notification_id, datetime.now(UTC), claim_token, error_code
            )
            logger.warning(
                "Notification delivery failed",
                extra={
                    "notification_id": str(notification_id),
                    "exception_type": type(exc).__name__,
                    "error_code": error_code,
                },
            )
        else:
            await store.amark_notification_sent(notification_id, claim_token)
    finally:
        await store.aclose()


async def _materialize_legacy_notification(transfer_id: uuid.UUID) -> None:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    try:
        await store.aensure_transfer_notification_outbox(transfer_id)
    finally:
        await store.aclose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.worker.send_transfer_notification",
    bind=True,
)
def send_transfer_notification(
    self: Any, notification_id: str, dispatch_token: str | None = None
) -> None:
    if dispatch_token is None:
        # Old queue messages carry transfer_id only. Persist their intent before
        # acknowledging them; the regular outbox dispatcher owns SMTP delivery.
        try:
            asyncio.run(_materialize_legacy_notification(uuid.UUID(notification_id)))
        except Exception as exc:
            raise self.retry(exc=exc, countdown=5) from exc
        return
    del self
    asyncio.run(_send_notification(uuid.UUID(notification_id), uuid.UUID(dispatch_token)))


# Celery treats max_retries=None in decorator options as "use the default".
# Setting the concrete task attribute keeps legacy queue messages until the
# database outbox can persist them, even if migrations or PostgreSQL are delayed.
cast(Any, send_transfer_notification).max_retries = None


async def _cleanup_orphaned_staging_areas() -> int:
    current_settings = get_settings()
    store = DatabaseStore(current_settings)
    try:
        active_transfer_ids = await store.alist_active_transfer_ids()
    finally:
        await store.aclose()
    return await asyncio.to_thread(
        cleanup_staging_areas,
        Path(current_settings.transfer_staging_path),
        active_transfer_ids=active_transfer_ids,
    )


@celery_app.task(name="app.worker.cleanup_orphaned_staging_areas")  # type: ignore[untyped-decorator]
def cleanup_orphaned_staging_areas() -> int:
    return asyncio.run(_cleanup_orphaned_staging_areas())
