import shutil
from pathlib import Path

import pytest

from kicad_rag.config import load_project


@pytest.fixture
def demo(tmp_path):
    original = Path(__file__).resolve().parents[1] / "examples" / "demo"
    root = tmp_path / "project"
    shutil.copytree(original, root, ignore=shutil.ignore_patterns(".rag", "reports"))
    return root, load_project(root)
