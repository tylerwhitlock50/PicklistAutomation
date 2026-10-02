"""Runtime settings read from the DB with env fallbacks, plus form validators."""
import json
import re
from datetime import datetime
from typing import Any, Optional

from picklist.config import EXCESS_PACKLIST_COST_DEFAULT, logger
from picklist.db import _int_setting, get_config_value
from picklist.domain import ffl_docs, identity
from picklist.services.notification_service import (
    email_address_is_valid,
    parse_recipient_addresses,
    recipients_are_valid,
)
from picklist.stores import shipping_store
from picklist.util import parse_bool


def get_excess_packlist_cost() -> float:
    """$ per avoidable extra packlist; settings page overrides env."""
    raw = get_config_value(
        "excess_packlist_cost_usd", "EXCESS_PACKLIST_COST_USD", str(EXCESS_PACKLIST_COST_DEFAULT)
    )
    try:
        value = float(str(raw).strip() or EXCESS_PACKLIST_COST_DEFAULT)
    except (TypeError, ValueError):
        return EXCESS_PACKLIST_COST_DEFAULT
    return value if value >= 0 else EXCESS_PACKLIST_COST_DEFAULT


def get_release_gate_mode() -> str:
    """off | advisory | enforced; new installs deliberately start advisory."""
    raw = get_config_value("release_gate_mode", "RELEASE_GATE_MODE", "advisory")
    mode = str(raw or "advisory").strip().lower()
    return mode if mode in {"off", "advisory", "enforced"} else "advisory"


def get_release_gate_due_override_days() -> int:
    raw = get_config_value(
        "release_gate_due_override_days", "RELEASE_GATE_DUE_OVERRIDE_DAYS", "1"
    )
    try:
        return max(0, min(int(str(raw).strip()), 30))
    except (TypeError, ValueError):
        return 1


def get_release_gate_min_ship_to_cooldown_days() -> int:
    """Global floor for the ship-to cooldown; protects every customer from
    same-day duplicate picklists even without an explicit policy."""
    raw = get_config_value(
        "release_gate_min_ship_to_cooldown_days",
        "RELEASE_GATE_MIN_SHIP_TO_COOLDOWN_DAYS",
        "1",
    )
    try:
        return max(0, min(int(str(raw).strip()), 30))
    except (TypeError, ValueError):
        return 1


def parse_release_gate_customer_policies(raw: Any) -> dict[str, dict[str, Any]]:
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError("Customer release policies must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Customer release policies must be a JSON object keyed by customer ID.")

    result: dict[str, dict[str, Any]] = {}
    for customer_id, policy in parsed.items():
        customer = str(customer_id or "").strip().upper()
        if not customer or not isinstance(policy, dict):
            raise ValueError("Each customer release policy must be an object.")
        normalized: dict[str, Any] = {}
        account_type = policy.get("account_type")
        if account_type not in (None, ""):
            account_type_value = str(account_type).strip().lower()
            if account_type_value not in {"major", "standard"}:
                raise ValueError(f"{customer}: account_type must be major or standard.")
            normalized["account_type"] = account_type_value
        accumulate = policy.get("accumulate", False)
        if not isinstance(accumulate, bool):
            raise ValueError(f"{customer}: accumulate must be true or false.")
        normalized["accumulate"] = accumulate
        try:
            min_guns = int(policy.get("min_guns", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{customer}: min_guns must be a whole number.") from exc
        if min_guns < 0:
            raise ValueError(f"{customer}: min_guns must be zero or greater.")
        normalized["min_guns"] = min_guns
        if "target_guns" in policy:
            try:
                target_guns = int(policy.get("target_guns", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{customer}: target_guns must be a whole number.") from exc
            if target_guns < 0:
                raise ValueError(f"{customer}: target_guns must be zero or greater.")
            normalized["target_guns"] = target_guns
        if "mix_orders" in policy:
            mix_orders = policy.get("mix_orders")
            if not isinstance(mix_orders, bool):
                raise ValueError(f"{customer}: mix_orders must be true or false.")
            normalized["mix_orders"] = mix_orders
        if "release_cadence" in policy:
            cadence = str(policy.get("release_cadence") or "").strip().lower()
            if cadence not in {"threshold", "daily", "completion"}:
                raise ValueError(
                    f"{customer}: release_cadence must be threshold, daily, or completion."
                )
            normalized["release_cadence"] = cadence
        if "daily_release_time" in policy:
            daily_release_time = str(policy.get("daily_release_time") or "").strip()
            try:
                parsed_release_time = datetime.strptime(daily_release_time, "%H:%M")
            except ValueError as exc:
                raise ValueError(
                    f"{customer}: daily_release_time must use 24-hour HH:MM format."
                ) from exc
            normalized["daily_release_time"] = parsed_release_time.strftime("%H:%M")
        if "max_hold_days" in policy:
            try:
                max_hold_days = int(policy.get("max_hold_days", 7))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{customer}: max_hold_days must be a whole number.") from exc
            if max_hold_days < 0 or max_hold_days > 90:
                raise ValueError(f"{customer}: max_hold_days must be from 0 to 90.")
            normalized["max_hold_days"] = max_hold_days
        if "ship_to_cooldown_days" in policy:
            try:
                cooldown_days = int(policy.get("ship_to_cooldown_days", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{customer}: ship_to_cooldown_days must be a whole number."
                ) from exc
            if cooldown_days < 0 or cooldown_days > 30:
                raise ValueError(
                    f"{customer}: ship_to_cooldown_days must be from 0 to 30."
                )
            normalized["ship_to_cooldown_days"] = cooldown_days
        sweep = policy.get("sweep_weekday")
        if sweep not in (None, ""):
            try:
                sweep_value = int(sweep)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{customer}: sweep_weekday must be 0 (Monday) through 6 (Sunday)."
                ) from exc
            if sweep_value not in range(7):
                raise ValueError(
                    f"{customer}: sweep_weekday must be 0 (Monday) through 6 (Sunday)."
                )
            normalized["sweep_weekday"] = sweep_value
        result[customer] = normalized
    return result


def get_release_gate_customer_policies() -> dict[str, dict[str, Any]]:
    raw = get_config_value(
        "release_gate_customer_policies_json",
        "RELEASE_GATE_CUSTOMER_POLICIES_JSON",
        "{}",
    )
    try:
        return parse_release_gate_customer_policies(raw)
    except ValueError as exc:
        logger.error("Invalid release-gate customer policy configuration: %s", exc)
        return {}


def ensure_release_gate_policy_version(changed_by: str = "system") -> dict[str, Any]:
    desired = {
        "mode": get_release_gate_mode(),
        "due_override_days": get_release_gate_due_override_days(),
        "customer_policies": get_release_gate_customer_policies(),
    }
    latest = shipping_store.latest_policy_config()
    if latest and all(latest.get(key) == value for key, value in desired.items()):
        return latest
    version = shipping_store.save_policy_config(changed_by=changed_by, **desired)
    return {"version": version, **desired, "changed_by": changed_by}


def get_operator_roster() -> list[dict[str, str]]:
    """Roster for the operator picker (Settings overrides OPERATOR_ROSTER_JSON)."""
    raw = get_config_value("operator_roster_json", "OPERATOR_ROSTER_JSON", "[]")
    try:
        return identity.parse_roster(raw)
    except ValueError as exc:
        logger.error("Invalid operator roster configuration: %s", exc)
        return []


def get_teams_digest_time() -> str:
    return (get_config_value("teams_digest_time", "TEAMS_DIGEST_TIME", "16:30") or "16:30").strip()


def get_max_runs_per_day() -> int:
    """Per-list generation budget; 0 = unlimited. Settings page overrides env."""
    raw = get_config_value("max_runs_per_day", "MAX_RUNS_PER_DAY", "0")
    try:
        return max(0, int(str(raw).strip() or 0))
    except (TypeError, ValueError):
        return 0


def validate_optional_chat_id(chat_id: str) -> Optional[str]:
    if chat_id and not re.fullmatch(r"-?\d+", chat_id):
        return "Chat ID must be numeric (optional leading -)."
    return None


def validate_optional_port(port_value: str) -> Optional[str]:
    if not port_value:
        return None
    if not re.fullmatch(r"\d+", port_value):
        return "SMTP port must be a number between 1 and 65535."
    port = int(port_value)
    if port < 1 or port > 65535:
        return "SMTP port must be a number between 1 and 65535."
    return None


def validate_optional_email(address: str, label: str) -> Optional[str]:
    if address and not email_address_is_valid(address):
        return f"{label} must be a valid email address."
    return None


def validate_optional_recipient_list(recipients_raw: str) -> Optional[str]:
    recipients = parse_recipient_addresses(recipients_raw)
    if recipients and not recipients_are_valid(recipients):
        invalid_recipient = next(
            (address for address in recipients if not email_address_is_valid(address)),
            recipients[0],
        )
        return f"Invalid email address: {invalid_recipient}"
    return None


def get_readiness_config() -> dict[str, Any]:
    codes = get_config_value("readiness_credit_check_codes", "READINESS_CREDIT_CHECK_CODES", "C") or "C"
    return {
        "require_ffl_doc": parse_bool(
            get_config_value("readiness_require_ffl_doc", "READINESS_REQUIRE_FFL_DOC", "true"),
            default=True,
        ),
        "credit_check_codes": tuple(
            code.strip().upper() for code in str(codes).split(",") if code.strip()
        ),
    }


def get_ffl_doc_config() -> dict[str, Any]:
    """Tier-2 (document OCR) settings. Off unless READINESS_OCR_ENABLED is true."""
    def _float(key: str, env: str, default: float) -> float:
        try:
            return float(get_config_value(key, env, str(default)) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": parse_bool(get_config_value("readiness_ocr_enabled", "READINESS_OCR_ENABLED", "false"), default=False),
        "path_map": get_config_value("document_path_map", "DOCUMENT_PATH_MAP", "") or "",
        "roots": get_config_value("document_roots", "DOCUMENT_ROOTS", "") or "",
        "ocr_dpi": _int_setting("readiness_ocr_dpi", "READINESS_OCR_DPI", ffl_docs.DEFAULT_OCR_DPI),
        "ocr_pages": _int_setting("readiness_ocr_pages", "READINESS_OCR_PAGES", ffl_docs.DEFAULT_OCR_PAGES),
        "max_docs_per_run": _int_setting("readiness_ocr_max_docs_per_run", "READINESS_OCR_MAX_DOCS_PER_RUN", ffl_docs.DEFAULT_MAX_DOCS_PER_RUN),
        "name_threshold": _float("readiness_ffl_name_threshold", "READINESS_FFL_NAME_THRESHOLD", ffl_docs.DEFAULT_NAME_THRESHOLD),
        "addr_threshold": _float("readiness_ffl_addr_threshold", "READINESS_FFL_ADDR_THRESHOLD", ffl_docs.DEFAULT_ADDR_THRESHOLD),
    }


def get_request_sla_overrides() -> dict[str, int]:
    raw = get_config_value("request_sla_hours_json", "REQUEST_SLA_HOURS_JSON", "{}") or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Invalid REQUEST_SLA_HOURS_JSON; using defaults")
        return {}
    out: dict[str, int] = {}
    for key, value in (data or {}).items():
        try:
            out[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return out
