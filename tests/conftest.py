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


@pytest.fixture
def fake_session(monkeypatch, resources_page_fixture):
    """Mock the Piazza session so the Resources page and downloads are local."""
    from fakes import STATUS, FakeSession

    from piazza_mcp import client as piazza_client

    piazza_client.reset()
    session = FakeSession(resources_page_fixture)
    monkeypatch.setattr(piazza_client, "_piazza_instance", lambda: object())
    monkeypatch.setattr(piazza_client, "user_status", lambda: STATUS)
    monkeypatch.setattr(piazza_client, "http_session", lambda: session)
    yield session
    piazza_client.reset()
