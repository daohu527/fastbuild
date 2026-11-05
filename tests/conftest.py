from pathlib import Path

import pytest

from fastbuild.demo import create_demo_inputs


@pytest.fixture(scope="session")
def demo_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("synthetic")
    return create_demo_inputs(root / "inputs")
