"""Telegram / SMTP delivery for run notifications and settings tests."""
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import requests

from picklist.config import logger
from picklist.db import get_config_value
from picklist.util import parse_bool


def send_telegram_notification(message: str, *, allow_fallback: bool = True) -> None:
    bot_token = get_config_value("telegram_bot_token", "TELEGRAM_BOT_TOKEN")
    chat_id = get_config_value("telegram_chat_id", "TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.info("Telegram credentials not configured; skipping Telegram notification.")
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.exception("Failed to send Telegram notification: %s", exc)
        if allow_fallback:
            send_email_notification(
                subject="Picklist alert: Telegram notification failed",
                body=(
                    "A Telegram notification could not be delivered.\n\n"
                    f"Error: {exc}\n\n"
                    "Original Telegram message:\n"
                    f"{message}"
                ),
                allow_fallback=False,
            )


def send_email_notification(
    subject: str,
    body: str,
    attachment: Optional[Path] = None,
    *,
    allow_fallback: bool = True,
) -> None:
    smtp_host = get_config_value("smtp_host", "SMTP_HOST")
    smtp_port_raw = get_config_value("smtp_port", "SMTP_PORT", default="587")
    smtp_user = get_config_value("smtp_user", "SMTP_USER")
    smtp_password = get_config_value("smtp_password", "SMTP_PASSWORD")
    smtp_sender = get_config_value("smtp_sender", "SMTP_SENDER")
    smtp_recipient = get_config_value("smtp_recipient", "SMTP_RECIPIENT")
    smtp_use_tls = parse_bool(
        get_config_value("smtp_use_tls", "SMTP_USE_TLS", default="true"),
        default=True,
    )
    recipients = [item.strip() for item in smtp_recipient.split(",")] if smtp_recipient else []
    recipients = [item for item in recipients if item]

    if not all([smtp_host, smtp_user, smtp_password, smtp_sender, recipients]):
        logger.info("SMTP credentials incomplete; skipping email notification.")
        return

    try:
        smtp_port = int(smtp_port_raw or "587")
    except ValueError:
        logger.warning("Invalid SMTP_PORT '%s'. Falling back to 587.", smtp_port_raw)
        smtp_port = 587

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    if attachment and attachment.exists():
        with attachment.open("rb") as file:
            msg.add_attachment(
                file.read(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=attachment.name,
            )

    try:
        import smtplib

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if smtp_use_tls:
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
            logger.info("Email sent to %s with subject '%s'.", msg["To"], subject)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send SMTP email: %s", exc)
        if allow_fallback:
            send_telegram_notification(
                (
                    "Picklist alert: SMTP notification failed.\n"
                    f"Error: {exc}\n"
                    f"Intended subject: {subject}"
                ),
                allow_fallback=False,
            )


def email_address_is_valid(address: str) -> bool:
    return bool(re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", address))


def send_telegram_notification_with_credentials(
    bot_token: str, chat_id: str, message: str
) -> tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Bot token and chat ID are required."

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        return True, "Telegram test message sent successfully."
    except requests.RequestException as exc:
        logger.exception("Telegram settings test failed: %s", exc)
        return False, f"Telegram request failed: {exc}"


def send_email_notification_with_config(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_sender: str,
    smtp_recipients: list[str],
    smtp_use_tls: bool,
    subject: str,
    body: str,
) -> tuple[bool, str]:
    if not all([smtp_host, smtp_user, smtp_password, smtp_sender, smtp_recipients]):
        return False, "SMTP host/user/password/sender/recipient are required."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_sender
    msg["To"] = ", ".join(smtp_recipients)
    msg.set_content(body)

    try:
        import smtplib

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if smtp_use_tls:
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True, "SMTP test email sent successfully."
    except Exception as exc:  # noqa: BLE001
        logger.exception("SMTP settings test failed: %s", exc)
        return False, f"SMTP test failed: {exc}"


def parse_recipient_addresses(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def recipients_are_valid(recipients: list[str]) -> bool:
    return all(email_address_is_valid(address) for address in recipients)
