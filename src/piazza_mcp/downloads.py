"""Save class Resources files to disk. Shared by the MCP server and the CLI.

This is the only code that writes local files. Writes go through a temp file
in the destination folder and are published without clobbering, so a failed,
oversized, or racing download never leaves a partial or replaced file.
"""

import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastmcp.exceptions import ToolError

from piazza_mcp import client, formatting

RESOURCE_MAX_BYTES = 200 * 1024 * 1024
FILENAME_MAX_CHARS = 200
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ ()-]+")
# A real extension starts with a letter, so titles like "v1.2" still get one.
_FILE_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}")

# mkstemp creates files as 0600; saved downloads should get normal permissions.
_UMASK = os.umask(0)
os.umask(_UMASK)
DOWNLOAD_FILE_MODE = 0o666 & ~_UMASK


class AlreadyExists(ToolError):
    """The target file exists and overwrite was not requested."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path} already exists. Pass overwrite=true to replace it.")


@dataclass(frozen=True)
class SavedResource:
    title: str
    path: Path
    size_bytes: int
    content_type: str

    def describe(self) -> str:
        size_kb = max(1, round(self.size_bytes / 1024))
        kind = self.content_type.split(";")[0].strip() or "unknown type"
        return f"Saved {self.title} to {self.path} ({size_kb} KB, {kind})"


def safe_filename(name: str, content_type: str, fallback: str) -> str:
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


def resolve_dest_dir(dest_dir: str) -> Path:
    """Absolute download folder, confined to PIAZZA_DOWNLOAD_ROOT when it is set."""
    directory = Path(dest_dir).expanduser()
    if not directory.is_absolute():
        raise ToolError(f"dest_dir must be an absolute path, got {dest_dir!r}.")
    directory = directory.resolve()
    if directory.exists() and not directory.is_dir():
        raise ToolError(f"dest_dir {directory} exists and is not a folder.")
    root_setting = os.environ.get("PIAZZA_DOWNLOAD_ROOT")
    if root_setting:
        root = Path(root_setting).expanduser().resolve()
        if directory != root and root not in directory.parents:
            raise ToolError(
                f"dest_dir {directory} is outside PIAZZA_DOWNLOAD_ROOT ({root})."
            )
    return directory


def publish(temp_path: str, target: Path, overwrite: bool) -> None:
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
        raise AlreadyExists(target) from None
    except OSError:
        # Some filesystems (exFAT, certain network shares) have no hard links.
        if target.exists():
            raise AlreadyExists(target) from None
        os.replace(temp_path, target)
        return
    os.unlink(temp_path)


def find_resource(network_id: str, resource_id: str) -> dict:
    for resource in client.resources(network_id):
        if resource.get("id") == resource_id:
            return resource
    raise ToolError(
        f"Resource {resource_id} was not found in class {network_id}. "
        "Call list_resources to see valid resource ids."
    )


def is_file(resource: dict) -> bool:
    config = resource.get("config") or {}
    return (config.get("resource_type") or "file") == "file"


def _stem_key(name: str) -> str:
    """Case-insensitive name without a real extension, for collision checks."""
    path = Path(name)
    stem = path.stem if _FILE_EXTENSION.fullmatch(path.suffix) else name
    return stem.casefold()


def unique_filenames(resources: list[dict]) -> dict[str, str]:
    """Filenames for file resources whose titles would save to the same file.

    Titles like "Notes", "notes", and "Notes.pdf" all become Notes.pdf (and
    collide outright on case-insensitive filesystems). Each member of such a
    group gets its resource id appended, which stays stable across runs so a
    re-sync still recognizes files it already saved. Returns {resource_id:
    filename} for colliding resources only; others keep their title.
    """
    groups: dict[str, list[dict]] = {}
    for resource in resources:
        if not is_file(resource):
            continue
        rid = resource.get("id") or ""
        name = safe_filename(formatting.resource_title(resource), "", rid)
        groups.setdefault(_stem_key(name), []).append(resource)
    renamed = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        for resource in members:
            rid = resource.get("id") or ""
            name = safe_filename(formatting.resource_title(resource), "", rid)
            path = Path(name)
            if _FILE_EXTENSION.fullmatch(path.suffix):
                renamed[rid] = f"{path.stem}-{rid}{path.suffix}"
            else:
                renamed[rid] = f"{name}-{rid}"
    return renamed


def save_resource(
    network_id: str,
    resource_id: str,
    dest_dir: str,
    filename: str | None = None,
    overwrite: bool = False,
    resource: dict | None = None,
) -> SavedResource:
    """Download one file resource into dest_dir and return what was saved.

    Pass resource (an entry from client.resources) to skip looking it up again.
    """
    if resource is None:
        resource = find_resource(network_id, resource_id)
    if not is_file(resource):
        link = (resource.get("content") or "").strip()
        raise ToolError(f"Resource {resource_id} is a link, not a file: {link}")
    directory = resolve_dest_dir(dest_dir)
    title = formatting.resource_title(resource)

    response = client.open_resource_download(network_id, resource_id)
    with response:
        content_type = response.headers.get("Content-Type", "")
        target = directory / safe_filename(filename or title, content_type, resource_id)

        # Stream into a temp file in the same folder, then publish it, so a
        # failed or oversized download never leaves a partial file behind.
        temp_path = None
        total = 0
        try:
            if target.exists() and not overwrite:
                raise AlreadyExists(target)
            directory.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".piazza-", suffix=".part")
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), DOWNLOAD_FILE_MODE)
                for chunk in response.iter_content(chunk_size=65_536):
                    total += len(chunk)
                    if total > RESOURCE_MAX_BYTES:
                        raise ToolError(
                            "Resource exceeds the "
                            f"{RESOURCE_MAX_BYTES // (1024 * 1024)} MB size limit."
                        )
                    handle.write(chunk)
            publish(temp_path, target, overwrite)
        except BaseException as exc:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)
            if isinstance(exc, Exception) and not isinstance(exc, ToolError):
                raise ToolError(f"Could not save {target}: {exc}") from exc
            raise

    return SavedResource(title=title, path=target, size_bytes=total, content_type=content_type)
