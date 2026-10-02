"""Trusted-client gate and CSRF protection."""
import ipaddress
import secrets
from functools import wraps
from typing import Optional

from flask import abort, request, session

from picklist.config import (
    ACCESS_ALLOWED_CIDRS,
    ACCESS_MODE,
    logger,
    SETTINGS_SESSION_KEY,
    TRUST_PROXY_HEADERS,
)


def get_client_ip() -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    candidate = request.remote_addr
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
    if not candidate:
        return None

    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def parse_networks(cidr_list: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in (item.strip() for item in cidr_list.split(",")):
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid CIDR in ACCESS_ALLOWED_CIDRS: %s", cidr)
    return networks


ALLOWED_NETWORKS = parse_networks(ACCESS_ALLOWED_CIDRS)


def request_is_allowed() -> bool:
    if ACCESS_MODE == "off":
        return True

    client_ip = get_client_ip()
    if not client_ip:
        return False

    if ACCESS_MODE == "cidr":
        if not ALLOWED_NETWORKS:
            logger.warning(
                "ACCESS_MODE=cidr but ACCESS_ALLOWED_CIDRS is empty; denying request."
            )
            return False
        return any(client_ip in network for network in ALLOWED_NETWORKS)

    # Default "private": allow local/private networks without user credentials.
    return client_ip.is_private or client_ip.is_loopback


def require_trusted_client(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if request_is_allowed():
            return view_func(*args, **kwargs)

        logger.warning(
            "Blocked request to %s from %s due to ACCESS_MODE=%s.",
            request.path,
            request.remote_addr,
            ACCESS_MODE,
        )
        abort(403)

    return wrapped


def get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def require_csrf(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        validate_csrf()
        return view_func(*args, **kwargs)

    return wrapped


def validate_csrf() -> None:
    sent_token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    session_token = session.get("_csrf_token")
    if not sent_token or not session_token:
        abort(400, description="Missing CSRF token.")
    if not secrets.compare_digest(sent_token, session_token):
        abort(400, description="Invalid CSRF token.")


def settings_access_granted() -> bool:
    return bool(session.get(SETTINGS_SESSION_KEY, False))
