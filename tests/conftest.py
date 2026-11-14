from __future__ import annotations

from pathlib import Path

import pytest

from voiceshop.config import load_settings
from voiceshop.context import build_context

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def settings():
    return load_settings(env={})


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def ctx(settings, db_path):
    return build_context(settings, db_path)
