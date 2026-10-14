import smtplib
import ssl
import uuid
from email.message import EmailMessage
from typing import Any

from app.services.smtp_config import SMTPServerConfig
from app.settings import Settings

_NOTIFIABLE = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        amount /= 1024
        if amount < 1024 or unit == "PB":
            return f"{amount:.1f} {unit}"
    return f"{amount:.1f} PB"


def render_transfer_email(
    transfer: Any,
    source_name: str,
    destination_name: str,
    app_base_url: str = "",
) -> tuple[str, str]:
    subject = f"Transferencia {transfer.public_id}: {transfer.status}"
    body = (
        "Resumen de transferencia\n\n"
        f"Identificador: {transfer.public_id}\n"
        f"Estado: {transfer.status}\n"
        f"Origen: {source_name}\n"
        f"Destino: {destination_name}\n"
        f"Datos transferidos: {format_bytes(transfer.bytes_transferred)}\n"
    )
    if transfer.created_at and transfer.completed_at:
        elapsed = max(0, int((transfer.completed_at - transfer.created_at).total_seconds()))
        body += f"Duración: {elapsed} segundos\n"
    if app_base_url:
        body += f"\nConsulta el historial en: {app_base_url.rstrip('/')}\n"
    return subject, body


async def enqueue_transfer_notification(store: Any, transfer_id: uuid.UUID, task: Any) -> bool:
    transfer = await store.aget_transfer(transfer_id)
    if (
        transfer is None
        or not transfer.send_email_notification
        or transfer.status not in _NOTIFIABLE
    ):
        return False
    task.delay(str(transfer_id))
    return True


def send_transfer_email(
    settings: Settings,
    smtp: SMTPServerConfig,
    recipient: str,
    transfer: Any,
    source_name: str,
    destination_name: str,
) -> None:
    if not smtp.host or not smtp.from_address:
        raise ValueError("SMTP settings are incomplete")
    subject, body = render_transfer_email(
        transfer, source_name, destination_name, settings.app_base_url
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp.from_address
    message["To"] = recipient
    message.set_content(body)
    try:
        with smtplib.SMTP(smtp.host, smtp.port, timeout=20) as client:
            if smtp.use_tls:
                client.starttls(context=ssl.create_default_context())
            if smtp.username:
                client.login(smtp.username, smtp.password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise NotificationDeliveryError("No fue posible entregar la notificación SMTP") from None


class NotificationDeliveryError(RuntimeError):
    pass
