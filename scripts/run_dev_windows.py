"""Run the Flask app locally on Windows.

Production runs on Linux under gunicorn (``picklist.app:app``) and does not use
this script. The scheduler's single-instance lock uses fcntl, which the
package skips automatically on Windows.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from picklist.app import app  # noqa: E402

app.run(
    host="127.0.0.1",
    port=int(os.getenv("PORT", "5000")),
    debug=False,
    use_reloader=False,
)
