"""Start a local-only synthetic training workspace, never the real ERP.

Run from the repository root after `cd operator-web; npm ci; npm run build`.
Demo credentials are printed only for this explicitly selected training mode.
"""
from pathlib import Path
import argparse
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from fulfillment.app import create_app
from fulfillment.model import users


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="Create a separate training database without deleting prior rehearsals")
    parser.add_argument("--port", type=int, default=5090)
    args = parser.parse_args()
    filename = f"fulfillment-demo-{uuid4().hex[:12]}.db" if args.fresh else "fulfillment-demo.db"
    path = Path(__file__).resolve().parent.parent / "data" / filename
    path.parent.mkdir(exist_ok=True)
    app = create_app({"MODE": "demo", "DATABASE_URL": "sqlite:///" + path.as_posix()})
    db = app.extensions["fulfillment_db"]
    db.initialize()
    with db.transaction() as conn:
        if not conn.execute(select(users.c.id).where(users.c.id == "DEMO")).first():
            conn.execute(users.insert().values(id="DEMO", name="Demo operator", role="supervisor", pin_hash=generate_password_hash("123456"), enabled=True))
    print(f"TRAINING ONLY: http://127.0.0.1:{args.port} | Operator DEMO | PIN 123456 | Database {path.name}", flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False)
