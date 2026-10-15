"""Canonical definition of the audited serialized-inventory universe.

Single source of truth for which bins hold serialized (firearm) inventory:
the MAIN cage bins plus the whole SHIPPING warehouse. Python consumers
(serial_history's expected-location check, and anything new) import these
constants instead of keeping their own copy.

The same filter is necessarily hardcoded in the SQL files — SQL Server has no
shared fragments and the files must stay runnable as-is in SSMS:

  - sql/audit_serialized_inventory.sql  (exact match)
  - sql/audit_locations_sync.sql        (exact match)
  - sql/audit_dwell_time.sql            (superset: adds staging/allocation
    bins, filtered at runtime by AUDIT_DWELL_LOCATIONS)

``missing_terms()`` is run against those files at app startup and a warning is
logged when a canonical bin no longer appears. To add a new serialized cage:
update the constants HERE first, then every SQL file the startup check flags.
"""

AUDITED_BINS: frozenset[tuple[str, str]] = frozenset(
    {
        ("MAIN", "C2"),
        ("MAIN", "C2-SERIALIZED"),
    }
)
AUDITED_WAREHOUSES: frozenset[str] = frozenset({"SHIPPING"})


def missing_terms(sql_text: str) -> list[str]:
    """Canonical bins/warehouses that do not appear (as quoted literals) in the SQL."""
    missing = []
    for wh, loc in sorted(AUDITED_BINS):
        if f"'{loc}'" not in sql_text:
            missing.append(f"{wh}/{loc}")
    for wh in sorted(AUDITED_WAREHOUSES):
        if f"'{wh}'" not in sql_text:
            missing.append(wh)
    return missing
