import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def post_fixture() -> dict:
    return json.loads((FIXTURES / "post.json").read_text())


@pytest.fixture
def feed_item_fixture() -> dict:
    return json.loads((FIXTURES / "feed_item.json").read_text())
