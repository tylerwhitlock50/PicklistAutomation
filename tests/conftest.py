"""Set isolation before test collection imports application configuration."""
import os
import tempfile
from pathlib import Path

# Some store tests import config before route tests can set their environment.
# A suite must never fall back to the application's working database.
_temporary = tempfile.TemporaryDirectory(prefix="picklist-tests-", ignore_cleanup_errors=True)
os.environ["RUN_HISTORY_DB_PATH"] = str(Path(_temporary.name) / "history.db")
os.environ["DATABASE_URL"] = ""
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ACCESS_MODE"] = "off"
