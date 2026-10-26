"""In-process MCP tests with the Piazza layer mocked. No network."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from piazza_mcp import client as piazza_client
from piazza_mcp.server import mcp

CLASSES = [
    {"name": "CS 252", "term": "Spring 2026", "num": "cs252", "nid": "abc123"},
    {"name": "MA 351", "term": "Spring 2026", "num": "ma351", "nid": "def456"},
]


@pytest.fixture
def fake_piazza(monkeypatch, feed_item_fixture, post_fixture):
    monkeypatch.setattr(piazza_client, "user_classes", lambda: CLASSES)
    monkeypatch.setattr(piazza_client, "folders", lambda nid: ["hw1", "exams"])
    monkeypatch.setattr(
        piazza_client,
        "feed",
        lambda nid, folder=None, unread_only=False: [feed_item_fixture] * 3,
    )
    monkeypatch.setattr(piazza_client, "search", lambda nid, query: [feed_item_fixture])
    monkeypatch.setattr(piazza_client, "post", lambda nid, nr: post_fixture)
    return feed_item_fixture


async def _call(tool: str, args: dict) -> str:
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool(tool, args)
        return result.content[0].text


async def test_list_classes(fake_piazza):
    text = await _call("list_classes", {})
    assert "CS 252 (Spring 2026)" in text
    assert "abc123" in text
    assert "def456" in text


async def test_get_folders(fake_piazza):
    text = await _call("get_folders", {"network_id": "abc123"})
    assert "- hw1" in text
    assert "- exams" in text


async def test_get_feed(fake_piazza):
    text = await _call("get_feed", {"network_id": "abc123", "limit": 2})
    assert text.count("@42") == 2
    assert "more posts available" in text


async def test_get_feed_offset_past_end(fake_piazza):
    text = await _call("get_feed", {"network_id": "abc123", "offset": 10})
    assert text == "No posts found."


async def test_search_posts(fake_piazza):
    text = await _call("search_posts", {"network_id": "abc123", "query": "hw1"})
    assert "@42" in text


async def test_get_post(fake_piazza):
    text = await _call("get_post", {"network_id": "abc123", "post_number": 42})
    assert "# @42 When is HW1 due?" in text
    assert "Instructor Answer" in text


async def test_get_daily_digest(fake_piazza):
    text = await _call("get_daily_digest", {"hours": 24})
    assert "# Piazza digest: last 24 hours" in text
    assert "CS 252" in text
    assert "MA 351" in text


async def test_get_daily_digest_window_excludes_old(fake_piazza, monkeypatch):
    old_item = dict(fake_piazza, modified="2020-01-01T00:00:00Z")
    monkeypatch.setattr(
        piazza_client, "feed", lambda nid, folder=None, unread_only=False: [old_item]
    )
    text = await _call("get_daily_digest", {"hours": 24})
    assert "No new activity in any class." in text


async def test_get_daily_digest_rejects_bad_hours(fake_piazza):
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError):
            await mcp_client.call_tool("get_daily_digest", {"hours": 0})


def test_missing_credentials_raise_tool_error(monkeypatch):
    monkeypatch.delenv("PIAZZA_EMAIL", raising=False)
    monkeypatch.delenv("PIAZZA_PASSWORD", raising=False)
    piazza_client.reset()
    with pytest.raises(ToolError, match="PIAZZA_EMAIL"):
        piazza_client.user_classes()


def test_password_falls_back_to_keychain(monkeypatch):
    import subprocess

    monkeypatch.setenv("PIAZZA_EMAIL", "me@school.edu")
    monkeypatch.delenv("PIAZZA_PASSWORD", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="s3cret\n", stderr="")

    logins = []
    monkeypatch.setattr(piazza_client.subprocess, "run", fake_run)
    monkeypatch.setattr(piazza_client.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        piazza_client.Piazza, "user_login", lambda self, email, password: logins.append((email, password))
    )
    piazza_client.reset()
    piazza_client._piazza_instance()
    piazza_client.reset()
    assert logins == [("me@school.edu", "s3cret")]
    assert calls[0][1:] == ["find-generic-password", "-s", "piazza-mcp", "-a", "me@school.edu", "-w"]


def test_keychain_miss_keeps_clear_error(monkeypatch):
    import subprocess

    monkeypatch.setenv("PIAZZA_EMAIL", "me@school.edu")
    monkeypatch.delenv("PIAZZA_PASSWORD", raising=False)
    monkeypatch.setattr(
        piazza_client.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found"),
    )
    piazza_client.reset()
    with pytest.raises(ToolError, match="Keychain"):
        piazza_client._piazza_instance()


class _FakeResponse:
    def __init__(self, chunks, content_type="application/pdf", status_code=200):
        self._chunks = chunks
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code

    def iter_content(self, chunk_size=65_536):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def test_read_attachment_blocks_non_piazza_host(monkeypatch):
    called = False

    def session():
        nonlocal called
        called = True

    monkeypatch.setattr(piazza_client, "http_session", session)
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="only"):
            await mcp_client.call_tool(
                "read_attachment", {"url": "http://169.254.169.254/latest/meta-data/"}
            )
    assert called is False  # blocked before any network access


async def test_read_attachment_enforces_size_limit(monkeypatch):
    from piazza_mcp import server

    monkeypatch.setattr(server, "ATTACHMENT_MAX_BYTES", 1024)
    oversized = _FakeResponse([b"x" * 2048], content_type="text/plain")
    monkeypatch.setattr(
        piazza_client, "http_session", lambda: type("S", (), {"get": lambda *a, **k: oversized})()
    )
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="size limit"):
            await mcp_client.call_tool(
                "read_attachment", {"url": "https://piazza.com/big.txt"}
            )


async def test_read_attachment_reads_piazza_text(monkeypatch):
    ok = _FakeResponse([b"hello ", b"world"], content_type="text/plain")
    monkeypatch.setattr(
        piazza_client, "http_session", lambda: type("S", (), {"get": lambda *a, **k: ok})()
    )
    text = await _call("read_attachment", {"url": "https://piazza.com/notes.txt"})
    assert text == "hello world"
