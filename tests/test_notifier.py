import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import requests

import notifier


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.gettempdir()) / f"picklist-notifier-{os.getpid()}-{id(self)}.db"
        notifier.initialize(lambda: sqlite3.connect(self.db_path))
        self.settings = {"teams_webhook_url": "https://example.test/hook"}
        self.sent = []
        self.emails = []

        def get_config_value(setting_key, env_key, default=None):
            return self.settings.get(setting_key, default)

        def transport(url, payload):
            self.sent.append((url, payload))

        def send_email(**kwargs):
            self.emails.append(kwargs)

        notifier.configure(
            get_config_value=get_config_value,
            send_email_notification=send_email,
            transport=transport,
        )

    def tearDown(self):
        notifier.configure(get_config_value=lambda *a, **k: None, transport=None)
        try:
            self.db_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    def test_card_shape(self):
        card = notifier.build_card(
            title="7 new holds",
            text="Sales owns 5",
            facts=[("Refresh", "now")],
            rows=[["SO-1", "FFL expired"], ["SO-2", "Credit"]],
            columns=["Order", "Hold"],
            link="https://ops.example/orders",
        )
        self.assertEqual(card["version"], "1.4")
        self.assertEqual(card["body"][0]["text"], "7 new holds")
        kinds = [block["type"] for block in card["body"]]
        self.assertEqual(kinds, ["TextBlock", "TextBlock", "FactSet", "ColumnSet", "ColumnSet", "ColumnSet"])
        self.assertEqual(card["actions"][0]["url"], "https://ops.example/orders")
        payload = notifier.build_payload(card)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["attachments"][0]["contentType"], "application/vnd.microsoft.card.adaptive")

    def test_rows_truncated_per_card(self):
        rows = [[f"SO-{i}"] for i in range(40)]
        card = notifier.build_card(title="t", rows=rows)
        column_sets = [b for b in card["body"] if b["type"] == "ColumnSet"]
        self.assertEqual(len(column_sets), notifier.ROWS_PER_CARD)
        self.assertIn("15 more", card["body"][-1]["text"])
        self.assertEqual(len(notifier.chunk_rows(rows, 25)), 2)
        self.assertEqual(notifier.chunk_rows([]), [[]])

    def test_send_records_and_is_idempotent(self):
        ok = notifier.send_teams_notification("shipped_digest", title="Shipped", event_key="digest:2026-10-01")
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        again = notifier.send_teams_notification("shipped_digest", title="Shipped", event_key="digest:2026-10-01")
        self.assertTrue(again)
        self.assertEqual(len(self.sent), 1, "second send with same key must be skipped")
        log = notifier.recent_log()
        self.assertEqual(log[0]["status"], "sent")

    def test_skips_when_unconfigured(self):
        self.settings.pop("teams_webhook_url")
        self.assertFalse(notifier.send_teams_notification("hold_created", title="x"))
        self.assertEqual(self.sent, [])
        self.assertEqual(notifier.recent_log()[0]["status"], "skipped")

    def test_disabled_event_skipped_unless_forced(self):
        self.settings["teams_enabled_events"] = "hold_created"
        self.assertFalse(notifier.send_teams_notification("picklist_run", title="x"))
        self.assertTrue(notifier.send_teams_notification("picklist_run", title="x", force=True))
        self.assertEqual(len(self.sent), 1)

    def test_enabled_events_parsing(self):
        self.assertEqual(notifier.parse_enabled_events("all"), set(notifier.TEAMS_EVENTS))
        self.assertEqual(notifier.parse_enabled_events("none"), set())
        self.assertEqual(notifier.parse_enabled_events(""), set(notifier.DEFAULT_ENABLED_EVENTS))
        self.assertEqual(
            notifier.parse_enabled_events("hold_created, bogus ,shipped_digest"),
            {"hold_created", "shipped_digest"},
        )

    def test_failure_falls_back_to_email(self):
        def failing(url, payload):
            raise requests.ConnectionError("boom")

        notifier.configure(
            get_config_value=lambda k, e, d=None: self.settings.get(k, d),
            send_email_notification=lambda **kw: self.emails.append(kw),
            transport=failing,
        )
        ok = notifier.send_teams_notification("hold_created", title="Holds", text="detail", event_key="k1")
        self.assertFalse(ok)
        self.assertEqual(len(self.emails), 1)
        self.assertIn("boom", self.emails[0]["body"])
        self.assertFalse(notifier.already_sent("teams", "k1"))
        self.assertEqual(notifier.recent_log()[0]["status"], "failed")

    def test_public_url(self):
        self.assertIsNone(notifier.public_url("orders"))
        self.settings["app_public_url"] = "https://ops.example/"
        self.assertEqual(notifier.public_url("/orders/SO-1"), "https://ops.example/orders/SO-1")
        self.assertEqual(notifier.public_url(), "https://ops.example")

    def test_send_test_validates_url(self):
        self.assertEqual(notifier.send_test("http://insecure")[0], False)
        ok, message = notifier.send_test("https://example.test/other")
        self.assertTrue(ok, message)
        self.assertEqual(self.sent[-1][0], "https://example.test/other")


if __name__ == "__main__":
    unittest.main()
