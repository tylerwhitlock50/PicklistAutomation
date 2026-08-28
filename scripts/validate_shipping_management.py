"""Read-only live validation for the Shipping scorecard and release gate.

Prints aggregate QA only: no connection string, order IDs, customer names, or serials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import dotenv_values
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import release_gate  # noqa: E402
import shipping_metrics  # noqa: E402


DEFAULT_COMPONENT_CODES = (
    "FG-COMP",
    "FG-STOCK",
    "FG-APPAREL",
    "COMPONENT",
    "FG-BASE",
    "FG-RING",
    "FG-BARREL",
    "FG-MUZZLE",
)


def _sql_rows(values: list[str], column: str) -> str:
    return "\n    UNION ALL\n    ".join(
        f"SELECT '{value.replace(chr(39), chr(39) * 2)}' AS {column}" for value in values
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if args.days not in {1, 7, 30, 90}:
        parser.error("--days must be 1, 7, 30, or 90")

    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    connection_string = str(config.get("MSSQL_CONNECTION_STRING") or "").strip()
    if not connection_string:
        raise SystemExit("MSSQL_CONNECTION_STRING is not configured")

    today = date.today()
    end = today + timedelta(days=1)
    start = today - timedelta(days=args.days - 1)
    metric_query = (ROOT / "sql" / "shipping_metrics.sql").read_text(encoding="utf-8")
    reconciliation_query = """
        WITH FirearmPacklists AS (
            SELECT
                s.PACKLIST_ID,
                COUNT(t.ID) AS GUNS
            FROM dbo.SHIPPER s WITH (NOLOCK)
            INNER JOIN dbo.SHIPPER_LINE sl WITH (NOLOCK)
                ON sl.PACKLIST_ID = s.PACKLIST_ID
            LEFT JOIN dbo.TRACE_INV_TRANS tit WITH (NOLOCK)
                ON tit.TRANSACTION_ID = sl.TRANSACTION_ID
            LEFT JOIN dbo.TRACE t WITH (NOLOCK)
                ON t.PART_ID = tit.PART_ID AND t.ID = tit.TRACE_ID
            WHERE s.SHIPPED_DATE >= :start_date
              AND s.SHIPPED_DATE < :end_date
              AND ISNULL(s.STATUS, '') NOT IN ('X', 'V')
            GROUP BY s.PACKLIST_ID
            HAVING COUNT(t.ID) > 0
        )
        SELECT COUNT(*) AS SHIPMENTS, SUM(GUNS) AS GUNS
        FROM FirearmPacklists
    """

    component_codes = [
        value.strip().upper()
        for value in str(
            config.get("SHORTAGE_PRODUCT_CODES") or ",".join(DEFAULT_COMPONENT_CODES)
        ).split(",")
        if value.strip()
    ]
    candidate_query = (ROOT / "sql" / "release_candidates.sql").read_text(
        encoding="utf-8"
    )
    candidate_query = (
        candidate_query.replace("__RELEASE_LOOKAHEAD_DAYS__", "10")
        .replace(
            "__COMPONENT_PRODUCT_CODES__",
            _sql_rows(component_codes, "PRODUCT_CODE"),
        )
        .replace(
            "__RELEASE_EXCLUDED_CUSTOMERS__",
            _sql_rows(["CA MARK"], "CUSTOMER_TERM"),
        )
    )

    engine = create_engine(connection_string, pool_pre_ping=True)
    timings: dict[str, float] = {}
    try:
        with engine.connect() as connection:
            started = time.perf_counter()
            metric_frame = pd.read_sql_query(
                text(metric_query),
                connection,
                params={
                    "query_start": (start - timedelta(days=args.days)).isoformat(),
                    "end_date": end.isoformat(),
                },
            )
            timings["metric_query_seconds"] = round(time.perf_counter() - started, 2)
            started = time.perf_counter()
            candidate_frame = pd.read_sql_query(candidate_query, connection)
            timings["release_query_seconds"] = round(time.perf_counter() - started, 2)
            started = time.perf_counter()
            reconciliation_frame = pd.read_sql_query(
                text(reconciliation_query),
                connection,
                params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )
            timings["reconciliation_query_seconds"] = round(
                time.perf_counter() - started, 2
            )
    finally:
        engine.dispose()

    scorecard = shipping_metrics.build_shipping_metrics(
        metric_frame.to_dict(orient="records"), start, end
    )
    gate = release_gate.evaluate_release_gate(
        candidate_frame.to_dict(orient="records"), today=today, mode="advisory"
    )
    independent = reconciliation_frame.iloc[0].to_dict()
    independent_guns = int(independent.get("GUNS") or 0)
    independent_shipments = int(independent.get("SHIPMENTS") or 0)
    result = {
        "period": scorecard["period"],
        "metric_rows": len(metric_frame.index),
        "metric_record_types": metric_frame["RECORD_TYPE"].value_counts().to_dict(),
        "cards": {key: value["value"] for key, value in scorecard["cards"].items()},
        "coverage": scorecard["coverage"],
        "release_candidate_rows": len(candidate_frame.index),
        "release_summary": gate["summary"],
        "release_reason_counts": dict(
            Counter(row["reason_code"] for row in gate["decisions"])
        ),
        "independent_reconciliation": {
            "guns": independent_guns,
            "shipments": independent_shipments,
            "guns_match": independent_guns
            == scorecard["cards"]["total_guns_shipped"]["value"],
            "shipments_match": independent_shipments
            == scorecard["cards"]["total_shipments"]["value"],
        },
        "timings": timings,
    }
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
