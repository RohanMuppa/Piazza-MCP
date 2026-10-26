"""MCP server for browsing Piazza course forums. Read-only on Piazza.

The only local side effect is download_resource writing into the folder it
is given (optionally confined by PIAZZA_DOWNLOAD_ROOT).
"""

import io
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pypdf import PdfReader

from piazza_mcp import client, formatting

mcp = FastMCP("piazza")

MAX_LIMIT = 50
DIGEST_ITEMS_PER_CLASS = 25
ATTACHMENT_MAX_CHARS = 50_000
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
RESOURCE_MAX_BYTES = 200 * 1024 * 1024
FILENAME_MAX_CHARS = 200
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ ()-]+")
# A real extension starts with a letter, so titles like "v1.2" still get one.
_FILE_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}")

# mkstemp creates files as 0600; saved downloads should get normal permissions.
_UMASK = os.umask(0)
os.umask(_UMASK)
_DOWNLOAD_FILE_MODE = 0o666 & ~_UMASK

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


def _safe_filename(name: str, content_type: str, fallback: str) -> str:
    """A single path component built from a resource title.

    Resource titles are instructor-written text, so strip anything that could
    leave dest_dir (separators, leading dots) and add an extension from the
    Content-Type when the title has none (Piazza titles look like "HW1_PDF").
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", name)
    cleaned = re.sub(r"\.{2,}", "_", cleaned).strip(" ._")
    cleaned = cleaned[:FILENAME_MAX_CHARS].strip(" ._") or fallback
    if not _FILE_EXTENSION.fullmatch(Path(cleaned).suffix):
        extension = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if extension:
            cleaned += extension
    return cleaned


def _resolve_dest_dir(dest_dir: str) -> Path:
    """Absolute download folder, confined to PIAZZA_DOWNLOAD_ROOT when it is set."""
    directory = Path(dest_dir).expanduser()
    if not directory.is_absolute():
        raise ToolError(f"dest_dir must be an absolute path, got {dest_dir!r}.")
    directory = directory.resolve()
    root_setting = os.environ.get("PIAZZA_DOWNLOAD_ROOT")
    if root_setting:
        root = Path(root_setting).expanduser().resolve()
        if directory != root and root not in directory.parents:
            raise ToolError(
                f"dest_dir {directory} is outside PIAZZA_DOWNLOAD_ROOT ({root})."
            )
    return directory


def _publish(temp_path: str, target: Path, overwrite: bool) -> None:
    """Move a finished temp file into place without clobbering by default.

    os.link fails if target already exists, so a file created mid-download
    (or by a parallel call with the same title) is never replaced.
    """
    if overwrite:
        os.replace(temp_path, target)
        return
    try:
        os.link(temp_path, target)
    except FileExistsError:
        raise ToolError(
            f"{target} already exists. Pass overwrite=true to replace it."
        ) from None
    except OSError:
        # Some filesystems (exFAT, certain network shares) have no hard links.
        if target.exists():
            raise ToolError(
                f"{target} already exists. Pass overwrite=true to replace it."
            ) from None
        os.replace(temp_path, target)
        return
    os.unlink(temp_path)


def _find_resource(network_id: str, resource_id: str) -> dict:
    for resource in client.resources(network_id):
        if resource.get("id") == resource_id:
            return resource
    raise ToolError(
        f"Resource {resource_id} was not found in class {network_id}. "
        "Call list_resources to see valid resource ids."
    )


def _class_label(cls: dict) -> str:
    name = cls.get("name") or cls.get("num") or "Unknown class"
    term = cls.get("term")
    network_id = cls.get("nid") or cls.get("id") or "?"
    label = f"{name} ({term})" if term else str(name)
    return f"{label} | network_id: {network_id}"


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
    return "\n".join(f"- {_class_label(cls)}" for cls in classes)


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
    resource = _find_resource(network_id, resource_id)
    config = resource.get("config") or {}
    if (config.get("resource_type") or "file") != "file":
        link = (resource.get("content") or "").strip()
        raise ToolError(f"Resource {resource_id} is a link, not a file: {link}")
    directory = _resolve_dest_dir(dest_dir)

    response = client.open_resource_download(network_id, resource_id)
    with response:
        content_type = response.headers.get("Content-Type", "")
        name = _safe_filename(
            filename or formatting.resource_title(resource), content_type, resource_id
        )
        target = directory / name
        if target.exists() and not overwrite:
            raise ToolError(f"{target} already exists. Pass overwrite=true to replace it.")

        # Stream into a temp file in the same folder, then publish it, so a
        # failed or oversized download never leaves a partial file behind.
        temp_path = None
        total = 0
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".piazza-", suffix=".part")
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), _DOWNLOAD_FILE_MODE)
                for chunk in response.iter_content(chunk_size=65_536):
                    total += len(chunk)
                    if total > RESOURCE_MAX_BYTES:
                        raise ToolError(
                            "Resource exceeds the "
                            f"{RESOURCE_MAX_BYTES // (1024 * 1024)} MB size limit."
                        )
                    handle.write(chunk)
            _publish(temp_path, target, overwrite)
        except BaseException as exc:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)
            if isinstance(exc, Exception) and not isinstance(exc, ToolError):
                raise ToolError(f"Could not save {target}: {exc}") from exc
            raise

    size_kb = max(1, round(total / 1024))
    kind = content_type.split(";")[0].strip() or "unknown type"
    return f"Saved {formatting.resource_title(resource)} to {target} ({size_kb} KB, {kind})"


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
        sections.append((_class_label(cls), recent[:DIGEST_ITEMS_PER_CLASS], truncated))
    return formatting.format_digest(sections, hours)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
