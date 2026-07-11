"""Run the Flask app locally on Windows.

app.py imports fcntl (Unix-only) for its single-instance lock, so native
Windows needs a no-op stub installed before the import. Production runs on
Linux/gunicorn and does not use this script.
"""
import os
import sys
import types
from pathlib import Path

if sys.platform == "win32":
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = fcntl.LOCK_UN = fcntl.LOCK_NB = fcntl.LOCK_SH = 0
    fcntl.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = fcntl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

import app as appmod  # noqa: E402

appmod.app.run(
    host="127.0.0.1",
    port=int(os.getenv("PORT", "5000")),
    debug=False,
    use_reloader=False,
)
