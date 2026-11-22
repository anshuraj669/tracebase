import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def indexed(tmp_path_factory):
    """Seed the demo repos into a temp NAS layout and index them once for the whole test session."""
    base = tmp_path_factory.mktemp("nas")
    seed = base / "seed.py"
    seed.write_text((ROOT / "demo" / "seed.py").read_text())
    subprocess.run([sys.executable, str(seed)], check=True, capture_output=True)

    from tracebase import db
    from tracebase.config import settings
    from tracebase.embeddings import vector_index

    settings.repos_dir = base / "volume1" / "repos"
    settings.data_dir = base / "data"
    settings.llm_provider = "none"
    db.reset_for_tests()
    vector_index.mark_dirty()

    from tracebase.code.indexer import index_all

    return index_all()
