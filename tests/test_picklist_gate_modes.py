import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd


os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
# Reuse an already-configured test DB (full-suite runs import app exactly once,
# with whichever module loads first winning) instead of pointing at a path this
# module would later have to delete out from under the shared app instance.
os.environ.setdefault(
    "RUN_HISTORY_DB_PATH",
    str(Path(tempfile.gettempdir()) / f"picklist-gate-modes-{os.getpid()}.db"),
)
if os.name == "nt":
    # app.py's production scheduler uses the Linux-only fcntl module. The
    # scheduler is disabled above; this shim only lets these tests import it.
    sys.modules.setdefault(
        "fcntl",
        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_: None),
    )

import app as app_module  # noqa: E402  (environment must be set before import)


def _picklist_frame():
    return pd.DataFrame(
        {"Cust Order ID": ["SO-1", "SO-2"], "Part ID": ["GUN-A", "GUN-B"]}
    )


def _advisory_payload():
    return {
        "mode": "advisory",
        "error": None,
        "released_orders": ["SO-1"],
        "decisions": [
            {
                "order_id": "SO-1",
                "decision": "RELEASE",
                "reason_code": "complete",
                "label": "SHIP NOW - complete",
            }
        ],
    }


class PicklistGateModeTests(unittest.TestCase):
    def _fetch(self, mode, gate_payload=None):
        gate_mock = MagicMock(return_value=gate_payload)
        with (
            patch.object(app_module, "load_query", return_value="SELECT 1"),
            patch.object(app_module, "get_release_gate_mode", return_value=mode),
            patch.object(app_module, "build_release_gate_payload", gate_mock),
            patch.object(app_module, "get_erp_engine", return_value=MagicMock()),
            patch.object(app_module.pd, "read_sql_query", return_value=_picklist_frame()),
        ):
            df = app_module.fetch_picklist_from_mssql("guns")
        return df, gate_mock

    def test_mode_off_returns_dataframe_without_gate_evaluation(self):
        df, gate_mock = self._fetch("off")
        self.assertIsNotNone(df)
        self.assertEqual(len(df.index), 2)
        self.assertNotIn("Release Gate", df.columns)
        gate_mock.assert_not_called()

    def test_advisory_mode_annotates_and_returns_dataframe(self):
        df, gate_mock = self._fetch("advisory", _advisory_payload())
        self.assertIsNotNone(df)
        gate_mock.assert_called_once()
        self.assertTrue(gate_mock.call_args.kwargs.get("persist"))
        self.assertIn("Release Gate", df.columns)
        self.assertEqual(list(df["Release Gate"]), ["RELEASE", "UNREVIEWED"])
        self.assertEqual(df.attrs["release_gate_payload"]["mode"], "advisory")


class GatePayloadPersistenceTests(unittest.TestCase):
    def _build(self, persist):
        policy = {
            "version": 1,
            "mode": "advisory",
            "due_override_days": 1,
            "customer_policies": {},
        }
        record_mock = MagicMock(return_value=42)
        sync_mock = MagicMock(return_value={"kept": 0})
        with (
            patch.object(app_module, "ensure_release_gate_policy_version", return_value=policy),
            patch.object(app_module, "_fetch_release_candidate_rows", return_value=[]),
            patch.object(app_module, "_fetch_release_serial_rows", return_value=[]),
            patch.object(app_module, "_fetch_release_shipto_history_rows", return_value=[]),
            patch.object(app_module.shipping_store, "record_evaluation", record_mock),
            patch.object(app_module.shipping_store, "sync_serial_reservations", sync_mock),
        ):
            payload = app_module.build_release_gate_payload(force=True, persist=persist)
        return payload, record_mock, sync_mock

    def test_dashboard_read_does_not_persist(self):
        payload, record_mock, sync_mock = self._build(persist=False)
        self.assertIsNone(payload["error"])
        self.assertIsNone(payload["evaluation_id"])
        record_mock.assert_not_called()
        sync_mock.assert_not_called()

    def test_picklist_boundary_persists(self):
        payload, record_mock, sync_mock = self._build(persist=True)
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["evaluation_id"], 42)
        record_mock.assert_called_once()
        sync_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
