"""pytest configuration for backend tests; isolates local databases."""
from __future__ import annotations

import sys
from pathlib import Path
import pytest

project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def isolate_default_local_sqlite_database(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Keep tests using the default runtime out of a developer's real memory DB."""
    monkeypatch.setenv("UNBOUND_CONVERSATION_DB", str(tmp_path / "unbound-test.sqlite3"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
