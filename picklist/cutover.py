"""Explicit operator cutover switch shared by HTTP and scheduled entrypoints."""
import os


def operator_cutover_enabled():
    return os.getenv("FULFILLMENT_OPERATOR_CUTOVER", "false").strip().lower() == "true"


def require_legacy_operator_workflow():
    if operator_cutover_enabled():
        raise RuntimeError("Operator work has moved to Dispatch. Legacy picking, exports and allocation runs are disabled.")
