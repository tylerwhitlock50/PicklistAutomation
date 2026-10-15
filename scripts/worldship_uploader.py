"""Upload a COMPLETE refreshed WorldShip CSV snapshot, including voided labels.

The API token is read from FULFILLMENT_LABEL_TOKEN, never a command-line argument.
Schedule every minute on the WorldShip station. A stale source file is rejected:
re-uploading an unchanged old file must not manufacture fresh void evidence.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path

import requests

REQUIRED = {"tracking", "site", "order", "packlist", "package", "void", "exported_at",
            "name", "address1", "address2", "address3", "city", "state", "postal", "country"}


def upload(path, origin, token, *, max_age=120, session=None):
    if not origin.startswith("https://"):
        raise ValueError("The uploader requires an HTTPS API origin.")
    if len(token) < 32:
        raise ValueError("Set a dedicated FULFILLMENT_LABEL_TOKEN (at least 32 characters).")
    source = Path(path)
    source_stat = source.stat()
    stamp = datetime.fromtimestamp(source_stat.st_mtime, timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if age < -60 or age > max_age:
        raise ValueError("WorldShip snapshot is stale or future-dated. Refresh it including void status before uploading.")
    raw = source.read_bytes()
    after = source.stat()
    if (after.st_mtime_ns, after.st_size) != (source_stat.st_mtime_ns, source_stat.st_size):
        raise ValueError("Snapshot changed while reading. Retry after WorldShip finishes exporting.")
    snapshot_hash = hashlib.sha256(raw).hexdigest()
    http = session or requests.Session()
    count = 0
    with io.StringIO(raw.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream)
        if not REQUIRED.issubset(reader.fieldnames or []):
            raise ValueError("Missing export columns: " + ", ".join(sorted(REQUIRED - set(reader.fieldnames or []))))
        for row in reader:
            void = row["void"].strip().upper()
            if void not in ("Y", "N", "TRUE", "FALSE", "1", "0"):
                raise ValueError("Unrecognized void flag; refusing to treat it as active.")
            payload = {k: row[k].strip() for k in ("tracking", "site", "order", "packlist", "package", "exported_at")}
            payload.update(void=void in ("Y", "TRUE", "1"), destination={k: row[k].strip() for k in ("name", "address1", "address2", "address3", "city", "state", "postal", "country")})
            identity = hashlib.sha256(json.dumps([snapshot_hash, stamp.isoformat(), payload], sort_keys=True).encode()).hexdigest()
            response = http.post(origin.rstrip("/") + "/api/v1/integrations/worldship/labels", json=payload,
                                 headers={"Authorization": "Bearer " + token, "Idempotency-Key": identity}, timeout=(5, 30))
            response.raise_for_status()
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()
    print(f"Uploaded {upload(args.snapshot, args.origin, os.getenv('FULFILLMENT_LABEL_TOKEN', ''))} package records.")


if __name__ == "__main__":
    main()
