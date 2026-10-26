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


@pytest.fixture
def resources_page_fixture() -> str:
    """Class profile HTML captured from a real Piazza Resources page (trimmed).

    The link entry with "];" in its title is synthetic, to exercise parsing.
    """
    return (FIXTURES / "resources_page.html").read_text()
