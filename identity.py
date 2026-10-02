"""Operator identity for the warehouse app.

v1 is a roster-backed name + team picker: no passwords, no SSO. The roster is a
JSON setting (``operator_roster_json``) edited on the Settings page. Browsers
remember the chosen name in localStorage (the same ``audit_operator_name`` key
the audit/pick/verify pages already use) and send it back on every request as
``X-Operator`` / ``X-Operator-Team`` headers or ``operator`` / ``operator_team``
form fields. Server-side, ``require_operator`` turns that into ``g.operator`` so
request/hold ownership and audit trails always carry a real person.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import wraps
from typing import Any, Callable, Iterable, Optional

TEAMS: tuple[str, ...] = ("sales", "shipping", "finance", "management")
TEAM_LABELS: dict[str, str] = {
    "sales": "Inside Sales",
    "shipping": "Shipping",
    "finance": "Finance",
    "management": "Management",
}
MAX_NAME_LEN = 60
MAX_ROSTER = 200


@dataclass(frozen=True)
class Operator:
    name: str
    team: str
    email: str = ""
    known: bool = False

    @property
    def team_label(self) -> str:
        return TEAM_LABELS.get(self.team, self.team.capitalize() if self.team else "")

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["team_label"] = self.team_label
        return data


_get_roster: Optional[Callable[[], list[dict[str, str]]]] = None


def configure(*, get_roster: Callable[[], list[dict[str, str]]]) -> None:
    """Inject the roster provider (app.py owns settings access)."""
    global _get_roster  # noqa: PLW0603
    _get_roster = get_roster


def _roster() -> list[dict[str, str]]:
    if _get_roster is None:
        return []
    try:
        return list(_get_roster() or [])
    except Exception:  # noqa: BLE001 - identity must never take a page down
        return []


def normalize_team(value: Any) -> str:
    team = str(value or "").strip().lower()
    return team if team in TEAMS else ""


def parse_roster(raw: Any) -> list[dict[str, str]]:
    """Validate a roster from JSON text or a list of dicts.

    Returns ``[{"name", "team", "email"}, ...]`` sorted by team then name.
    Raises ``ValueError`` with a message suitable for a settings flash.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Roster must be valid JSON: {exc.msg}.") from exc
    else:
        data = raw
    if not isinstance(data, list):
        raise ValueError("Roster must be a JSON list of people.")
    if len(data) > MAX_ROSTER:
        raise ValueError(f"Roster is limited to {MAX_ROSTER} people.")

    members: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Roster entry {index} must be an object with name and team.")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"Roster entry {index} is missing a name.")
        if len(name) > MAX_NAME_LEN:
            raise ValueError(f"Roster name '{name[:20]}…' is longer than {MAX_NAME_LEN} characters.")
        team = normalize_team(entry.get("team"))
        if not team:
            raise ValueError(
                f"{name}: team must be one of {', '.join(TEAMS)}."
            )
        email = str(entry.get("email") or "").strip()
        if email and ("@" not in email or " " in email):
            raise ValueError(f"{name}: email '{email}' does not look like an address.")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"{name} appears more than once in the roster.")
        seen.add(key)
        members.append({"name": name, "team": team, "email": email})

    members.sort(key=lambda member: (TEAMS.index(member["team"]), member["name"].casefold()))
    return members


def find_member(roster: Iterable[dict[str, str]], name: Any) -> Optional[dict[str, str]]:
    key = str(name or "").strip().casefold()
    if not key:
        return None
    for member in roster:
        if str(member.get("name") or "").strip().casefold() == key:
            return member
    return None


def _first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def resolve_operator(req: Any, roster: Optional[Iterable[dict[str, str]]] = None) -> Optional[Operator]:
    """Resolve the acting operator from a Flask-like request.

    Precedence: ``X-Operator`` header, then ``operator`` form field, then
    ``operator`` key in a JSON body. Unknown names are returned with
    ``known=False`` so callers can decide whether to accept them.
    """
    members = list(roster) if roster is not None else _roster()
    headers = getattr(req, "headers", {}) or {}
    form = getattr(req, "form", {}) or {}
    cookies = getattr(req, "cookies", {}) or {}
    body: dict[str, Any] = {}
    get_json = getattr(req, "get_json", None)
    if callable(get_json):
        try:
            parsed = get_json(silent=True)
        except Exception:  # noqa: BLE001
            parsed = None
        if isinstance(parsed, dict):
            body = parsed

    name = _first(headers.get("X-Operator"), form.get("operator"), body.get("operator"), cookies.get("ops_operator"))
    if not name:
        return None
    member = find_member(members, name)
    if member:
        return Operator(
            name=member["name"],
            team=member["team"],
            email=member.get("email", ""),
            known=True,
        )
    team = normalize_team(
        _first(
            headers.get("X-Operator-Team"), form.get("operator_team"), body.get("operator_team"),
            cookies.get("ops_operator_team"),
        )
    )
    return Operator(name=name[:MAX_NAME_LEN], team=team, email="", known=False)


def require_operator(view_func):
    """Flask decorator: resolve the operator or abort.

    400 when no name was sent (the UI picker was never used); 403 when a roster
    exists and the name is not on it. Stores the result on ``flask.g.operator``.
    """
    from flask import abort, g, request  # local import keeps the module testable without Flask

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        roster = _roster()
        operator = resolve_operator(request, roster)
        if operator is None:
            abort(400, description="Pick your name in the top bar before doing that.")
        if roster and not operator.known:
            abort(403, description=f"'{operator.name}' is not on the operator roster.")
        g.operator = operator
        return view_func(*args, **kwargs)

    return wrapped


def current_operator() -> Optional[Operator]:
    """The operator for the active request, resolving lazily when not decorated."""
    from flask import g, has_request_context, request

    if not has_request_context():
        return None
    cached = getattr(g, "operator", None)
    if cached is not None:
        return cached
    operator = resolve_operator(request, _roster())
    if operator is not None:
        g.operator = operator
    return operator


def actor_label(operator: Optional[Operator], fallback: str = "unknown") -> str:
    """Compact "Name (team)" string for audit columns."""
    if operator is None:
        return fallback
    return f"{operator.name} ({operator.team})" if operator.team else operator.name
