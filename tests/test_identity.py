import json
import unittest
from types import SimpleNamespace

import identity


def fake_request(headers=None, form=None, body=None):
    return SimpleNamespace(
        headers=headers or {},
        form=form or {},
        get_json=lambda silent=True: body,
    )


ROSTER = [
    {"name": "Holly", "team": "sales", "email": "holly@example.com"},
    {"name": "Richard", "team": "shipping", "email": ""},
]


class ParseRosterTests(unittest.TestCase):
    def test_blank_is_empty(self):
        self.assertEqual(identity.parse_roster(""), [])
        self.assertEqual(identity.parse_roster(None), [])

    def test_valid_json_sorted_by_team_then_name(self):
        raw = json.dumps(
            [
                {"name": "Zed", "team": "shipping"},
                {"name": "amy", "team": "shipping"},
                {"name": "Holly", "team": "SALES", "email": "h@x.com"},
            ]
        )
        members = identity.parse_roster(raw)
        self.assertEqual([m["name"] for m in members], ["Holly", "amy", "Zed"])
        self.assertEqual(members[0]["team"], "sales")
        self.assertEqual(members[1]["email"], "")

    def test_rejects_bad_json(self):
        with self.assertRaises(ValueError):
            identity.parse_roster("{not json")

    def test_rejects_non_list(self):
        with self.assertRaises(ValueError):
            identity.parse_roster('{"name": "x"}')

    def test_rejects_unknown_team(self):
        with self.assertRaises(ValueError) as ctx:
            identity.parse_roster('[{"name": "Pat", "team": "warehouse"}]')
        self.assertIn("Pat", str(ctx.exception))

    def test_rejects_duplicate_names_case_insensitive(self):
        with self.assertRaises(ValueError):
            identity.parse_roster(
                '[{"name": "Holly", "team": "sales"}, {"name": "holly", "team": "shipping"}]'
            )

    def test_rejects_bad_email(self):
        with self.assertRaises(ValueError):
            identity.parse_roster('[{"name": "Holly", "team": "sales", "email": "nope"}]')


class ResolveOperatorTests(unittest.TestCase):
    def test_header_wins_over_form(self):
        req = fake_request(headers={"X-Operator": "Holly"}, form={"operator": "Richard"})
        op = identity.resolve_operator(req, ROSTER)
        self.assertEqual((op.name, op.team, op.known), ("Holly", "sales", True))
        self.assertEqual(op.email, "holly@example.com")

    def test_form_then_json(self):
        self.assertEqual(
            identity.resolve_operator(fake_request(form={"operator": "richard"}), ROSTER).team,
            "shipping",
        )
        self.assertEqual(
            identity.resolve_operator(fake_request(body={"operator": "Richard"}), ROSTER).name,
            "Richard",
        )

    def test_missing_name_is_none(self):
        self.assertIsNone(identity.resolve_operator(fake_request(), ROSTER))

    def test_unknown_name_keeps_supplied_team_when_valid(self):
        req = fake_request(headers={"X-Operator": "Guest", "X-Operator-Team": "Finance"})
        op = identity.resolve_operator(req, ROSTER)
        self.assertEqual((op.name, op.team, op.known), ("Guest", "finance", False))

    def test_unknown_team_is_blank(self):
        op = identity.resolve_operator(
            fake_request(form={"operator": "Guest", "operator_team": "nope"}), []
        )
        self.assertEqual(op.team, "")
        self.assertEqual(op.team_label, "")

    def test_actor_label(self):
        self.assertEqual(identity.actor_label(identity.Operator("Holly", "sales")), "Holly (sales)")
        self.assertEqual(identity.actor_label(None), "unknown")
        self.assertEqual(identity.actor_label(identity.Operator("Guest", "")), "Guest")


class RequireOperatorTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask, g, jsonify

        self.app = Flask(__name__)
        self.roster = list(ROSTER)
        self._previous_roster_provider = identity._get_roster
        identity.configure(get_roster=lambda: self.roster)

        @self.app.post("/act")
        @identity.require_operator
        def act():
            return jsonify(g.operator.as_dict())

        self.client = self.app.test_client()

    def tearDown(self):
        identity.configure(get_roster=self._previous_roster_provider or (lambda: []))

    def test_missing_operator_is_400(self):
        self.assertEqual(self.client.post("/act").status_code, 400)

    def test_unknown_operator_is_403_when_roster_exists(self):
        self.assertEqual(self.client.post("/act", headers={"X-Operator": "Nobody"}).status_code, 403)

    def test_unknown_operator_allowed_when_roster_empty(self):
        self.roster.clear()
        response = self.client.post("/act", data={"operator": "Nobody", "operator_team": "shipping"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["known"], False)

    def test_known_operator_sets_g(self):
        response = self.client.post("/act", headers={"X-Operator": "holly"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["team_label"], "Inside Sales")


if __name__ == "__main__":
    unittest.main()
