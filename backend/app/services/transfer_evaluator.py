from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.memory_store import ConnectionRecord, TransferRecord
from app.services.provider_factory import StorageProviderFactory
from app.services.source_inventory import (
    InventoryCancelledError,
    InventoryPreflightError,
    InventoryProgress,
    SourceInventoryScanner,
)
from app.services.transfer_evaluation import EvaluationClaim
from app.settings import Settings

logger = logging.getLogger(__name__)

_EVALUATION_HEARTBEAT_SECONDS = 5
_EVALUATION_TIMEOUT_SECONDS = 600
_EVALUATION_RETRY_DELAYS = (60, 120)
_SUCCESSFUL_PARENT_STATUSES = {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
_SUPPORTED_DESTINATIONS = {"GOOGLE_DRIVE", "MEGA", "SEAFILE", "NEXTCLOUD"}


class TransferEvaluator:
    """Run one read-only inventory evaluation under a durable claim."""

    def __init__(
        self,
        settings: Settings,
        store: Any,
        publish: Callable[[TransferRecord], Awaitable[None]],
        *,
        provider_factory: Callable[[ConnectionRecord], Any] | Any | None = None,
        now_fn: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        timeout_seconds: int = _EVALUATION_TIMEOUT_SECONDS,
        heartbeat_seconds: int = _EVALUATION_HEARTBEAT_SECONDS,
    ) -> None:
        self.settings = settings
        self.store = store
        self.publish = publish
        self.provider_factory = provider_factory or StorageProviderFactory(settings)
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.monotonic_fn = monotonic_fn
        self.timeout_seconds = timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.scanner = SourceInventoryScanner(settings)

    def _create_provider(self, connection: ConnectionRecord) -> Any:
        factory = self.provider_factory
        if hasattr(factory, "create"):
            return factory.create(connection)
        return factory(connection)

    async def _publish_current(self, transfer_id: uuid.UUID) -> None:
        record = await self.store.aget_transfer(transfer_id)
        if record is None:
            return
        try:
            await self.publish(record)
        except Exception as exc:
            logger.warning(
                "Evaluation state publication failed",
                extra={
                    "transfer_id": str(transfer_id),
                    "exception_type": type(exc).__name__,
                },
            )

    @staticmethod
    def _supported_pair(source_type: str, destination_type: str) -> bool:
        if source_type == destination_type == "MOCK":
            return True
        if destination_type not in _SUPPORTED_DESTINATIONS:
            return False
        return not (source_type == destination_type == "MEGA")

    async def _finish(
        self,
        claim: EvaluationClaim,
        *,
        status: str,
        progress: InventoryProgress,
        error_code: str | None = None,
        message: str | None = None,
        retry_at: datetime | None = None,
    ) -> bool:
        saved = await self.store.afinish_evaluation(
            claim,
            status=status,
            progress=progress,
            error_code=error_code,
            message=message,
            retry_at=retry_at,
            now=self.now_fn(),
        )
        if saved:
            await self._publish_current(claim.transfer_id)
        return bool(saved)

    async def execute(self, claim: EvaluationClaim) -> None:
        async with self.store.transfer_execution_lock(claim.transfer_id) as acquired:
            if not acquired:
                return
            transfer = await self.store.aget_transfer(claim.transfer_id)
            if (
                transfer is None
                or transfer.status not in {"SCHEDULED", "QUEUED"}
                or transfer.dispatch_token is not None
                or transfer.execution_started_at is not None
                or transfer.cancel_requested_at is not None
                or transfer.evaluation is None
                or transfer.evaluation.status != "EVALUATING"
                or transfer.evaluation.generation != claim.generation
                or transfer.evaluation.claim_token != claim.claim_token
            ):
                return
            await self._publish_current(claim.transfer_id)

            source_connection = await self.store.aget_connection(transfer.source_connection_id)
            destination_connection = await self.store.aget_connection(
                transfer.destination_connection_id
            )
            if source_connection is None or destination_connection is None:
                await self._finish(
                    claim,
                    status="BLOCKED",
                    progress=InventoryProgress(
                        files_found=transfer.evaluation.files_found or 0,
                        count_complete=False,
                    ),
                    error_code="CONNECTION_NOT_FOUND",
                    message="No se puede evaluar porque falta una conexión de almacenamiento.",
                )
                return
            if not self._supported_pair(
                source_connection.provider_type, destination_connection.provider_type
            ):
                await self._finish(
                    claim,
                    status="BLOCKED",
                    progress=InventoryProgress(
                        files_found=transfer.evaluation.files_found or 0,
                        count_complete=False,
                    ),
                    error_code="PROVIDER_PAIR_UNSUPPORTED",
                    message="Esta combinación de conexiones no admite transferencias.",
                )
                return
            if (
                source_connection.status != "ACTIVE"
                or destination_connection.status != "ACTIVE"
                or source_connection.credential_version != transfer.source_credential_version
                or destination_connection.credential_version
                != transfer.destination_credential_version
                or (
                    transfer.evaluation.source_credential_version is not None
                    and transfer.evaluation.source_credential_version
                    != source_connection.credential_version
                )
                or (
                    transfer.evaluation.destination_credential_version is not None
                    and transfer.evaluation.destination_credential_version
                    != destination_connection.credential_version
                )
            ):
                await self._finish(
                    claim,
                    status="BLOCKED",
                    progress=InventoryProgress(
                        files_found=transfer.evaluation.files_found or 0,
                        count_complete=False,
                    ),
                    error_code="BLOCKED_CONNECTION_CHANGED",
                    message=(
                        "Una conexión o sus credenciales cambiaron. "
                        "Solicita una nueva evaluación."
                    ),
                )
                return

            if transfer.depends_on_transfer_id is not None:
                parent = await self.store.aget_transfer(transfer.depends_on_transfer_id)
                if parent is None or parent.status not in _SUCCESSFUL_PARENT_STATUSES:
                    await self._finish(
                        claim,
                        status="WAITING_PARENT",
                        progress=InventoryProgress(
                            files_found=transfer.evaluation.files_found or 0,
                            count_complete=False,
                        ),
                        error_code=None,
                        message="La selección se evaluará cuando termine la transferencia padre.",
                    )
                    return

            lost_claim = asyncio.Event()
            stop_heartbeat = asyncio.Event()
            progress = InventoryProgress(
                files_found=transfer.evaluation.files_found or 0,
                count_complete=False,
            )
            last_persisted_at: float | None = None

            async def heartbeat() -> None:
                while True:
                    try:
                        await asyncio.wait_for(
                            stop_heartbeat.wait(), timeout=self.heartbeat_seconds
                        )
                        return
                    except TimeoutError:
                        try:
                            active = await self.store.aheartbeat_evaluation(claim, self.now_fn())
                        except Exception as exc:
                            logger.warning(
                                "Evaluation heartbeat failed",
                                extra={
                                    "transfer_id": str(claim.transfer_id),
                                    "exception_type": type(exc).__name__,
                                },
                            )
                            continue
                        if not active:
                            lost_claim.set()
                            return

            async def should_cancel() -> bool:
                return lost_claim.is_set() or await self.store.ais_transfer_cancel_requested(
                    claim.transfer_id
                )

            async def on_progress(value: InventoryProgress) -> None:
                nonlocal last_persisted_at, progress
                progress = value
                elapsed = self.monotonic_fn()
                due_to_publish = (
                    last_persisted_at is None
                    or elapsed - last_persisted_at >= 1.0
                    or value.count_complete
                )
                if not due_to_publish:
                    return
                saved = await self.store.asave_evaluation_progress(claim, value, self.now_fn())
                if not saved:
                    lost_claim.set()
                    raise InventoryCancelledError("Evaluation claim no longer active")
                last_persisted_at = elapsed
                await self._publish_current(claim.transfer_id)

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                maximum_files = transfer.evaluation.max_files or (
                    self.settings.smb_transfer_max_files
                    if source_connection.provider_type == "SMB"
                    else self.settings.transfer_max_files
                )
                provider = self._create_provider(source_connection)
                async with AsyncExitStack() as stack:
                    if source_connection.provider_type == "MEGA":
                        provider = await stack.enter_async_context(
                            provider.open_session(
                                transfer_id=transfer.id,
                                should_cancel=should_cancel,
                            )
                        )
                    async with asyncio.timeout(self.timeout_seconds):
                        inventory = await self.scanner.scan(
                            provider,
                            transfer,
                            allow_unknown_size=source_connection.provider_type == "GOOGLE_DRIVE",
                            maximum_files=maximum_files,
                            on_progress=on_progress,
                            should_cancel=should_cancel,
                        )
                progress = InventoryProgress(
                    files_found=len(inventory.files),
                    count_complete=True,
                )
                await self._finish(
                    claim,
                    status="APPROVED",
                    progress=progress,
                )
            except InventoryPreflightError as exc:
                await self._finish(
                    claim,
                    status="BLOCKED",
                    progress=exc.progress,
                    error_code=exc.error_code,
                    message=exc.message,
                )
            except InventoryCancelledError:
                return
            except TimeoutError:
                await self._retry_or_fail(
                    claim,
                    progress,
                    error_code="EVALUATION_TIMEOUT",
                )
            except Exception as exc:
                logger.warning(
                    "Source evaluation failed",
                    extra={
                        "transfer_id": str(claim.transfer_id),
                        "exception_type": type(exc).__name__,
                    },
                )
                await self._retry_or_fail(
                    claim,
                    progress,
                    error_code="EVALUATION_PROVIDER_UNAVAILABLE",
                )
            finally:
                stop_heartbeat.set()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _retry_or_fail(
        self,
        claim: EvaluationClaim,
        progress: InventoryProgress,
        *,
        error_code: str,
    ) -> None:
        transfer = await self.store.aget_transfer(claim.transfer_id)
        evaluation = transfer.evaluation if transfer is not None else None
        attempts = evaluation.attempt_count if evaluation is not None else 3
        if attempts < 3:
            delay = _EVALUATION_RETRY_DELAYS[min(attempts - 1, len(_EVALUATION_RETRY_DELAYS) - 1)]
            status = "PENDING"
            retry_at = self.now_fn() + timedelta(seconds=delay)
            message = "La revisión no pudo consultar el origen; se volverá a intentar."
        else:
            status = "ERROR"
            retry_at = None
            message = "No se pudo evaluar la selección tras tres intentos. Vuelve a evaluar."
        await self._finish(
            claim,
            status=status,
            progress=progress,
            error_code=error_code,
            message=message,
            retry_at=retry_at,
        )
