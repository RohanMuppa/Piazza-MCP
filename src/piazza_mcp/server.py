"""MCP server for browsing Piazza course forums. Read-only on Piazza.

The only local side effect is download_resource writing into the folder it
is given (optionally confined by PIAZZA_DOWNLOAD_ROOT).
"""

import io
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pypdf import PdfReader

from piazza_mcp import client, downloads, formatting

mcp = FastMCP("piazza")

MAX_LIMIT = 50
DIGEST_ITEMS_PER_CLASS = 25
ATTACHMENT_MAX_CHARS = 50_000
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024

# Attachments are served from piazza.com and its content CDN/storage. Restrict
# downloads to these hosts so a crafted link in a post cannot turn the
# authenticated session into a probe of arbitrary or internal addresses.
ALLOWED_ATTACHMENT_HOST_SUFFIXES = (
    "piazza.com",
    "piazza-resources.s3.amazonaws.com",
    "amazonaws.com",
    "cloudfront.net",
)


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _require_allowed_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not host or not any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ALLOWED_ATTACHMENT_HOST_SUFFIXES
    ):
        raise ToolError(
            f"Refusing to download from {host or url!r}. read_attachment only "
            "fetches Piazza-hosted files (piazza.com and its storage CDN)."
        )


@mcp.tool
def list_classes() -> str:
    """List the Piazza classes you are enrolled in.

    Returns one line per class with its name, term, and network_id.
    Every other piazza tool takes one of these network_id values.
    Call this first if you do not know the network_id.
    """
    classes = client.user_classes()
    if not classes:
        return "No enrolled Piazza classes found."
    return "\n".join(f"- {formatting.class_label(cls)}" for cls in classes)


@mcp.tool
def get_folders(network_id: str) -> str:
    """List the folder names of one class, for filtering get_feed by folder.

    network_id comes from list_classes.
    """
    names = client.folders(network_id)
    if not names:
        return "This class has no folders."
    return "\n".join(f"- {name}" for name in names)


@mcp.tool
def get_feed(
    network_id: str,
    limit: int = 20,
    offset: int = 0,
    folder: str | None = None,
    unread_only: bool = False,
) -> str:
    """Browse recent posts in one class, newest first.

    Returns compact summaries (post number, type, subject, answer status,
    snippet). Use get_post with a post number for the full thread.
    folder filters to one folder (see get_folders); it takes precedence
    over unread_only. limit is capped at 50; page with offset.
    """
    limit = _clamp_limit(limit)
    items = client.feed(network_id, folder=folder, unread_only=unread_only)
    page = items[offset : offset + limit]
    return formatting.format_feed_items(page, has_more=len(items) > offset + limit)


@mcp.tool
def search_posts(network_id: str, query: str, limit: int = 20) -> str:
    """Search posts in one class by keyword.

    Piazza search is AND-only keyword matching with no ranking, so prefer
    short queries of 1-2 words ("midterm", "hw3 grading"). Returns the
    same compact summaries as get_feed; use get_post for full threads.
    """
    limit = _clamp_limit(limit)
    items = client.search(network_id, query)
    page = items[:limit]
    return formatting.format_feed_items(page, has_more=len(items) > limit)


@mcp.tool
def get_post(network_id: str, post_number: int) -> str:
    """Read one full post thread by its post number (the @123 number).

    Returns the question, instructor and student answers with endorsement
    counts, and all followup discussions, rendered as markdown. Attachment
    links in the content can be read with read_attachment.
    """
    return formatting.format_post(client.post(network_id, post_number))


@mcp.tool
def read_attachment(url: str) -> str:
    """Download a file linked in a Piazza post and extract its text.

    Use the link URLs that appear in get_post output. Relative URLs
    (starting with /) are resolved against piazza.com. Only Piazza-hosted
    files are fetched. Supports PDF and plain-text files only.
    """
    if url.startswith("/"):
        url = "https://piazza.com" + url
    _require_allowed_host(url)
    try:
        response = client.http_session().get(url, timeout=30, stream=True)
    except Exception as exc:
        raise ToolError(f"Download failed for {url}: {exc}") from exc
    with response:
        if response.status_code != 200:
            raise ToolError(f"Download failed for {url}: HTTP {response.status_code}")

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65_536):
            chunks.append(chunk)
            total += len(chunk)
            if total > ATTACHMENT_MAX_BYTES:
                raise ToolError(
                    "Attachment exceeds the "
                    f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB size limit."
                )
        body = b"".join(chunks)
        content_type = response.headers.get("Content-Type", "")

    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(body))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise ToolError(f"Could not extract PDF text: {exc}") from exc
    elif content_type.startswith("text/") or "json" in content_type:
        text = body.decode("utf-8", errors="replace")
    else:
        raise ToolError(
            f"Unsupported attachment type {content_type!r}; "
            "only PDF and plain-text files are supported."
        )

    text = text.strip()
    if not text:
        return "(no extractable text in this attachment)"
    if len(text) > ATTACHMENT_MAX_CHARS:
        return text[:ATTACHMENT_MAX_CHARS] + "\n\n(truncated)"
    return text


@mcp.tool
def list_resources(network_id: str) -> str:
    """List the files and links on a class's Resources page.

    Instructors post handouts, homework, solutions, and reference sheets
    here, separately from the Q&A feed. Returns one line per resource,
    grouped by section, with the resource_id that download_resource takes.
    """
    return formatting.format_resources(client.resources(network_id))


@mcp.tool
def download_resource(
    network_id: str,
    resource_id: str,
    dest_dir: str,
    filename: str | None = None,
    overwrite: bool = False,
) -> str:
    """Save one file from a class's Resources page to a local folder.

    resource_id comes from list_resources. dest_dir must be an absolute
    path on the machine running this server (inside PIAZZA_DOWNLOAD_ROOT
    when that is set); it is created if missing. The file is named after
    the resource title (with an extension added from its type) unless
    filename is given. Existing files are kept unless overwrite is true.
    Link resources have no file to download.
    """
    return downloads.save_resource(
        network_id, resource_id, dest_dir, filename=filename, overwrite=overwrite
    ).describe()


@mcp.tool
def get_daily_digest(hours: int = 24, unread_only: bool = False) -> str:
    """Cross-class activity review: everything new on Piazza recently.

    Checks every enrolled class for posts created or updated within the
    last `hours` hours and returns one digest grouped by class, flagging
    unanswered questions. Each item shows its post number so get_post can
    fetch the full thread. Designed to be called by a daily scheduled task.
    """
    if hours < 1:
        raise ToolError("hours must be at least 1")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    sections = []
    for cls in client.user_classes():
        network_id = cls.get("nid") or cls.get("id")
        if not network_id:
            continue
        items = client.feed(network_id, unread_only=unread_only)
        recent = [
            item
            for item in items
            if (modified := formatting.parse_time(item.get("modified") or item.get("updated")))
            and modified >= cutoff
        ]
        truncated = len(recent) > DIGEST_ITEMS_PER_CLASS
        sections.append((formatting.class_label(cls), recent[:DIGEST_ITEMS_PER_CLASS], truncated))
    return formatting.format_digest(sections, hours)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
