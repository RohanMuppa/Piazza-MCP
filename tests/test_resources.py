"""Resources page tools with the Piazza session mocked. No network."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from piazza_mcp import client as piazza_client
from piazza_mcp import server
from piazza_mcp.server import mcp

NID = "msyx97enxo77kr"
STATUS = {
    "networks": [
        {
            "id": NID,
            "school_ext": "purdue",
            "term_key": "fall2026",
            "short_number": "wlece36200",
            "folders": ["hw1"],
        }
    ]
}
PAGE_URL = "https://piazza.com/purdue/fall2026/wlece36200/resources"


class _FakeResponse:
    def __init__(self, status_code=200, text="", chunks=(), content_type="application/pdf"):
        self.status_code = status_code
        self.text = text
        self._chunks = list(chunks)
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size=65_536):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    """Serves the Resources page and downloads. Queued pages/downloads are
    returned first (one per request), then the defaults."""

    def __init__(self, page_html, download=None):
        self.page_html = page_html
        self.download = download or _FakeResponse(chunks=[b"%PDF-1.4 ", b"body"])
        self.queued_pages = []
        self.queued_downloads = []
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url == PAGE_URL:
            html = self.queued_pages.pop(0) if self.queued_pages else self.page_html
            return _FakeResponse(text=html)
        if "/class_profile/get_resource/" in url:
            return self.queued_downloads.pop(0) if self.queued_downloads else self.download
        return _FakeResponse(status_code=404)


def _download_count(session):
    return sum("/class_profile/get_resource/" in url for url in session.urls)


@pytest.fixture
def fake_session(monkeypatch, resources_page_fixture):
    piazza_client.reset()
    session = _FakeSession(resources_page_fixture)
    monkeypatch.setattr(piazza_client, "_piazza_instance", lambda: object())
    monkeypatch.setattr(piazza_client, "user_status", lambda: STATUS)
    monkeypatch.setattr(piazza_client, "http_session", lambda: session)
    yield session
    piazza_client.reset()


async def _call(tool: str, args: dict) -> str:
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool(tool, args)
        return result.content[0].text


def test_parse_resources_handles_bracket_in_title(resources_page_fixture):
    parsed = piazza_client.parse_resources(resources_page_fixture)
    assert [r["id"] for r in parsed] == [
        "mtgo11puylh1cd",
        "mu1pcbthj905n2",
        "mvieovrvaql6o1",
        "lnk0000000001",
    ]
    assert parsed[3]["subject"] == "Course site ];"


def test_parse_resources_missing_returns_none():
    assert piazza_client.parse_resources("<html>login page</html>") is None
    assert piazza_client.parse_resources("var RESOURCES = {not json};") is None


def test_class_profile_url(fake_session):
    assert piazza_client.class_profile_url(NID) == PAGE_URL


def test_class_profile_url_missing_fields(monkeypatch):
    monkeypatch.setattr(piazza_client, "user_status", lambda: {"networks": [{"id": NID}]})
    with pytest.raises(ToolError, match="cannot be located"):
        piazza_client.class_profile_url(NID)


def test_ids_with_path_characters_are_rejected():
    with pytest.raises(ToolError, match="Invalid resource_id"):
        piazza_client.resource_download_url(NID, "../../etc")
    with pytest.raises(ToolError, match="Invalid network_id"):
        piazza_client.class_profile_url("abc/def")


def test_resources_expired_session_relogins_once(monkeypatch, fake_session):
    fake_session.page_html = "<html>please log in</html>"
    logins = []
    monkeypatch.setattr(piazza_client, "_piazza_instance", lambda: logins.append(1) or object())
    with pytest.raises(ToolError, match="No Resources data"):
        piazza_client.resources(NID)
    assert len(logins) == 2  # first try, then one re-login retry


async def test_list_resources(fake_session):
    text = await _call("list_resources", {"network_id": NID})
    assert "## homework" in text
    assert "## homework_solutions" in text
    assert "**riscv.pdf** [file] resource_id: mvieovrvaql6o1" in text
    assert "https://engineering.purdue.edu/ece362/" in text


async def test_download_resource_adds_extension(fake_session, tmp_path):
    text = await _call(
        "download_resource",
        {"network_id": NID, "resource_id": "mtgo11puylh1cd", "dest_dir": str(tmp_path)},
    )
    saved = tmp_path / "Homework_1_PDF.pdf"
    assert saved.read_bytes() == b"%PDF-1.4 body"
    assert str(saved) in text
    assert fake_session.urls[-1].endswith(f"/class_profile/get_resource/{NID}/mtgo11puylh1cd")
    assert not list(tmp_path.glob(".piazza-*"))


async def test_download_resource_keeps_existing_extension(fake_session, tmp_path):
    await _call(
        "download_resource",
        {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(tmp_path)},
    )
    assert (tmp_path / "riscv.pdf").exists()


async def test_download_resource_custom_filename_is_sanitized(fake_session, tmp_path):
    await _call(
        "download_resource",
        {
            "network_id": NID,
            "resource_id": "mvieovrvaql6o1",
            "dest_dir": str(tmp_path),
            "filename": "../../green card",
        },
    )
    assert [p.name for p in tmp_path.iterdir()] == ["green card.pdf"]


async def test_download_resource_refuses_overwrite(fake_session, tmp_path):
    (tmp_path / "riscv.pdf").write_bytes(b"old")
    args = {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(tmp_path)}
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="already exists"):
            await mcp_client.call_tool("download_resource", args)
    assert (tmp_path / "riscv.pdf").read_bytes() == b"old"

    await _call("download_resource", {**args, "overwrite": True})
    assert (tmp_path / "riscv.pdf").read_bytes() == b"%PDF-1.4 body"


async def test_download_resource_rejects_link(fake_session, tmp_path):
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="is a link"):
            await mcp_client.call_tool(
                "download_resource",
                {"network_id": NID, "resource_id": "lnk0000000001", "dest_dir": str(tmp_path)},
            )


async def test_download_resource_unknown_id(fake_session, tmp_path):
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="list_resources"):
            await mcp_client.call_tool(
                "download_resource",
                {"network_id": NID, "resource_id": "nope123", "dest_dir": str(tmp_path)},
            )


async def test_download_resource_requires_absolute_dest(fake_session):
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="absolute"):
            await mcp_client.call_tool(
                "download_resource",
                {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": "rel/dir"},
            )


async def test_download_resource_size_limit_leaves_no_file(fake_session, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESOURCE_MAX_BYTES", 8)
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="size limit"):
            await mcp_client.call_tool(
                "download_resource",
                {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(tmp_path)},
            )
    assert list(tmp_path.iterdir()) == []


async def test_download_resource_http_error(fake_session, tmp_path):
    fake_session.download = _FakeResponse(status_code=403)
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="HTTP 403"):
            await mcp_client.call_tool(
                "download_resource",
                {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(tmp_path)},
            )
    assert _download_count(fake_session) == 2  # one re-login retry, then give up


def test_resources_retry_succeeds_after_relogin(fake_session):
    fake_session.queued_pages = ["<html>please log in</html>"]
    resources = piazza_client.resources(NID)
    assert len(resources) == 4


async def test_list_resources_empty_class(fake_session):
    fake_session.page_html = "<script>var RESOURCES = [];\nvar USER_TYPE = 1;</script>"
    text = await _call("list_resources", {"network_id": NID})
    assert text == "This class has no Resources."


async def test_download_login_page_retries_then_saves_file(fake_session, tmp_path):
    fake_session.queued_downloads = [
        _FakeResponse(chunks=[b"<html>log in</html>"], content_type="text/html")
    ]
    await _call(
        "download_resource",
        {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(tmp_path)},
    )
    assert (tmp_path / "riscv.pdf").read_bytes() == b"%PDF-1.4 body"
    assert _download_count(fake_session) == 2


async def test_download_html_on_retry_is_accepted(fake_session, tmp_path):
    fake_session.download = _FakeResponse(chunks=[b"<h1>notes</h1>"], content_type="text/html")
    await _call(
        "download_resource",
        {"network_id": NID, "resource_id": "mtgo11puylh1cd", "dest_dir": str(tmp_path)},
    )
    assert (tmp_path / "Homework_1_PDF.html").read_bytes() == b"<h1>notes</h1>"


async def test_download_resource_uses_normal_file_mode(fake_session, tmp_path):
    await _call(
        "download_resource",
        {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(tmp_path)},
    )
    mode = (tmp_path / "riscv.pdf").stat().st_mode & 0o777
    assert mode == server._DOWNLOAD_FILE_MODE
    assert mode & 0o044  # group/other can read under a normal umask


async def test_download_stream_error_leaves_no_file(fake_session, tmp_path):
    fake_session.download = _FakeResponse(chunks=[b"part", ConnectionError("reset by peer")])
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="Could not save"):
            await mcp_client.call_tool(
                "download_resource",
                {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(tmp_path)},
            )
    assert list(tmp_path.iterdir()) == []


async def test_download_root_confines_dest_dir(fake_session, tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv("PIAZZA_DOWNLOAD_ROOT", str(root))
    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="outside PIAZZA_DOWNLOAD_ROOT"):
            await mcp_client.call_tool(
                "download_resource",
                {
                    "network_id": NID,
                    "resource_id": "mvieovrvaql6o1",
                    "dest_dir": str(root / ".." / "elsewhere"),
                },
            )
    await _call(
        "download_resource",
        {"network_id": NID, "resource_id": "mvieovrvaql6o1", "dest_dir": str(root / "ece362")},
    )
    assert (root / "ece362" / "riscv.pdf").exists()
    assert not (tmp_path / "elsewhere").exists()


def test_publish_never_clobbers_a_file_created_mid_download(tmp_path):
    temp = tmp_path / ".piazza-x.part"
    temp.write_bytes(b"new")
    target = tmp_path / "riscv.pdf"
    target.write_bytes(b"appeared during download")
    with pytest.raises(ToolError, match="already exists"):
        server._publish(str(temp), target, overwrite=False)
    assert target.read_bytes() == b"appeared during download"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Homework_1_PDF", "Homework_1_PDF.pdf"),
        ("riscv.pdf", "riscv.pdf"),
        ("Lecture v1.2", "Lecture v1.2.pdf"),
        ("../../etc/passwd", "etc_passwd.pdf"),
        ("ラボ", "fallbackid.pdf"),
        ("x" * 199 + " .tail", "x" * 199 + ".pdf"),
    ],
)
def test_safe_filename(title, expected):
    assert server._safe_filename(title, "application/pdf", "fallbackid") == expected
