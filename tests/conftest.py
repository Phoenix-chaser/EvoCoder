from pathlib import Path

import pytest

from evocoder.contracts import Settings
from evocoder.store import Store


@pytest.fixture
def settings(tmp_path: Path):
    return Settings(_env_file=None, state_dir=tmp_path / "state")


@pytest.fixture
def store(settings):
    return Store(settings.state_dir)
