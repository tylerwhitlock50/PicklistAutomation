"""Feature rollout flags."""

from picklist.db import get_setting
from picklist.util import parse_bool


# Feature rollout toggles. Disabled features are removed from the top nav and
# their pages/APIs are blocked, so new modules can be introduced one at a time.
FEATURE_FLAGS = {
    "shipping": {
        "setting_key": "feature_shipping_enabled",
        "label": "Shipping",
        "path_prefixes": (
            "/shipping",
            "/pick",
            "/verify",
            "/api/shipping",
            "/api/pick",
            "/api/verify",
        ),
    },
    "audit": {
        "setting_key": "feature_audit_enabled",
        "label": "Audit",
        "path_prefixes": ("/audit", "/api/audit"),
    },
    "serial": {
        "setting_key": "feature_serial_enabled",
        "label": "Serial Lookup",
        "path_prefixes": ("/serial-history", "/api/serial-history"),
    },
    "allocation": {
        "setting_key": "feature_allocation_enabled",
        "label": "Allocation",
        "path_prefixes": ("/allocation", "/api/allocation"),
    },
    "orders": {
        "setting_key": "feature_orders_enabled",
        "label": "Orders",
        "path_prefixes": (
            "/orders",
            "/api/orders",
            "/api/readiness",
            "/shipments",
            "/api/shipments",
            "/stock",
            "/api/stock",
            "/api/shipping/digest",
        ),
    },
    "requests": {
        "setting_key": "feature_requests_enabled",
        "label": "Requests",
        "path_prefixes": ("/requests", "/api/requests", "/api/holds"),
    },
}


def feature_enabled(feature: str) -> bool:
    return parse_bool(get_setting(FEATURE_FLAGS[feature]["setting_key"]), default=True)


def get_feature_flags() -> dict[str, bool]:
    return {name: feature_enabled(name) for name in FEATURE_FLAGS}
