"""Authenticated access to Piazza. All piazza-api usage lives here.

Piazza has no official API; piazza-api drives the internal JSON-RPC
endpoint with a cookie session. This module owns the lazy login, a short
TTL cache so tools do not hammer the endpoint, and one re-login retry
for expired sessions.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

# requests ships with piazza-api, whose session this module drives.
import requests
from fastmcp.exceptions import ToolError
from piazza_api import Piazza
from piazza_api.exceptions import AuthenticationError, RequestError
from piazza_api.network import FolderFilter, UnreadFilter

CACHE_TTL_SECONDS = 60
FEED_FETCH_LIMIT = 200
PIAZZA_BASE_URL = "https://piazza.com"

# Network ids and resource ids are short lowercase alphanumeric strings. They
# are interpolated into URL paths, so anything else is rejected up front.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
_RESOURCES_VAR = re.compile(r"var\s+RESOURCES\s*=\s*")

_lock = threading.RLock()
_piazza = None
_cache = {}


def reset():
    """Drop the session and caches. Used on auth failure and in tests."""
    global _piazza
    with _lock:
        _piazza = None
        _cache.clear()


KEYCHAIN_SERVICE = "piazza-mcp"


def _keychain_password(email):
    """Password saved in the macOS Keychain under service "piazza-mcp", if any.

    Lets the MCP config hold no secret: store it once with
    security add-generic-password -s piazza-mcp -a <email> -w
    """
    security = shutil.which("security") or "/usr/bin/security"
    if not os.path.exists(security):
        return None
    try:
        result = subprocess.run(
            [security, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    password = result.stdout.rstrip("\n")
    return password if result.returncode == 0 and password else None


def _piazza_instance():
    global _piazza
    with _lock:
        if _piazza is None:
            email = os.environ.get("PIAZZA_EMAIL")
            password = os.environ.get("PIAZZA_PASSWORD")
            if email and not password:
                password = _keychain_password(email)
            if not email or not password:
                raise ToolError(
                    "PIAZZA_EMAIL and PIAZZA_PASSWORD environment variables must be "
                    "set to your Piazza login credentials (or save the password in "
                    f"the macOS Keychain under service {KEYCHAIN_SERVICE!r})."
                )
            piazza = Piazza()
            try:
                piazza.user_login(email=email, password=password)
            except AuthenticationError as exc:
                raise ToolError(f"Piazza login failed: {exc}") from exc
            except requests.RequestException as exc:
                raise ToolError(f"Could not reach Piazza to log in: {exc}") from exc
            _piazza = piazza
        return _piazza


def _call(fn):
    """Run fn against a live session; re-login once if the session expired."""
    try:
        return fn(_piazza_instance())
    except ToolError:
        raise
    except requests.RequestException as exc:
        raise ToolError(f"Could not reach Piazza: {exc}") from exc
    except (RequestError, AuthenticationError):
        reset()
        try:
            return fn(_piazza_instance())
        except ToolError:
            raise
        except requests.RequestException as exc:
            raise ToolError(f"Could not reach Piazza: {exc}") from exc
        except (RequestError, AuthenticationError) as exc:
            raise ToolError(f"Piazza request failed: {exc}") from exc


def _cached(key, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    value = _call(fn)
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


def user_classes():
    """Enrolled classes as dicts with name, term, num, and nid keys."""
    return _cached("classes", lambda p: p.get_user_classes())


def user_status():
    """Raw user status, which includes per-network config such as folders."""
    return _cached("status", lambda p: p.get_user_status())


def _network(network_id):
    """The user-status entry for one enrolled class."""
    status = user_status()
    for network in status.get("networks", []):
        if network.get("id") == network_id:
            return network
    raise ToolError(
        f"Class {network_id} was not found in your enrolled classes. "
        "Call list_classes to see valid network ids."
    )


def folders(network_id):
    """Folder names configured for one class."""
    return _network(network_id).get("folders") or []


def require_id(value, kind):
    """Reject ids that are not plain alphanumerics before they reach a URL."""
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise ToolError(f"Invalid {kind} {value!r}; expected letters and digits only.")
    return value


def class_profile_url(network_id):
    """URL of the class profile page that lists the class Resources.

    Piazza serves Resources from /<school_ext>/<term_key>/<short_number>/resources
    (e.g. /purdue/fall2026/wlece36200/resources), not from the Q&A page.
    """
    require_id(network_id, "network_id")
    network = _network(network_id)
    parts = [network.get(key) for key in ("school_ext", "term_key", "short_number")]
    if not all(parts):
        raise ToolError(
            f"Piazza did not report the school, term, and course number for class "
            f"{network_id}, so its Resources page cannot be located."
        )
    return f"{PIAZZA_BASE_URL}/{parts[0]}/{parts[1]}/{parts[2]}/resources"


def parse_resources(html):
    """Extract the RESOURCES array that Piazza embeds in the class profile page."""
    match = _RESOURCES_VAR.search(html)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(html, match.end())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) else None


def resources(network_id):
    """Raw Resources entries for one class (files and links)."""
    require_id(network_id, "network_id")

    def fetch(_piazza):
        # Built inside fetch so a re-login retry uses fresh user status.
        url = class_profile_url(network_id)
        try:
            response = http_session().get(url, timeout=30)
        except Exception as exc:
            raise ToolError(f"Could not load the Resources page {url}: {exc}") from exc
        if response.status_code in (401, 403):
            raise RequestError(f"Piazza refused the Resources page {url}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise ToolError(
                f"Could not load the Resources page {url}: HTTP {response.status_code}"
            )
        parsed = parse_resources(response.text)
        if parsed is None:
            # Usually an expired session served the login page instead. Raising
            # RequestError lets _call re-login once before giving up.
            raise RequestError(
                f"No Resources data found on {url}. The session may have expired "
                "or Piazza changed its page layout."
            )
        return parsed

    return _cached(("resources", network_id), fetch)


def resource_download_url(network_id, resource_id):
    """Authenticated download URL for one file resource."""
    require_id(network_id, "network_id")
    require_id(resource_id, "resource_id")
    return f"{PIAZZA_BASE_URL}/class_profile/get_resource/{network_id}/{resource_id}"


def open_resource_download(network_id, resource_id):
    """Start a streamed download of one file resource; the caller closes it.

    An expired session shows up as 401/403 or as Piazza's HTML login page
    served with 200, so either one triggers a single re-login retry. HTML on
    the retry is accepted, since an instructor may have uploaded an HTML file.
    """
    url = resource_download_url(network_id, resource_id)
    for attempt in range(2):
        try:
            response = http_session().get(url, timeout=60, stream=True)
        except Exception as exc:
            raise ToolError(f"Download failed for {url}: {exc}") from exc
        refused = response.status_code in (401, 403)
        looks_like_login = response.status_code == 200 and response.headers.get(
            "Content-Type", ""
        ).startswith("text/html")
        if attempt == 0 and (refused or looks_like_login):
            response.close()
            reset()
            continue
        if response.status_code != 200:
            response.close()
            raise ToolError(f"Download failed for {url}: HTTP {response.status_code}")
        return response


def feed(network_id, folder=None, unread_only=False):
    """Raw feed items for a class, newest first.

    folder takes precedence over unread_only when both are given.
    """
    key = ("feed", network_id, folder, unread_only)

    def fetch(piazza):
        network = piazza.network(network_id)
        if folder:
            result = network.get_filtered_feed(FolderFilter(folder))
        elif unread_only:
            result = network.get_filtered_feed(UnreadFilter())
        else:
            result = network.get_feed(limit=FEED_FETCH_LIMIT, offset=0)
        if isinstance(result, dict):
            return result.get("feed") or []
        return result or []

    return _cached(key, fetch)


def search(network_id, query):
    """Raw search results for a keyword query in one class."""

    def fetch(piazza):
        result = piazza.network(network_id).search_feed(query)
        if isinstance(result, dict):
            return result.get("feed") or []
        return result or []

    return _call(fetch)


def post(network_id, post_number):
    """Full post thread by its visible post number (the @123 number)."""

    def fetch(piazza):
        return piazza.network(network_id).get_post(post_number)

    try:
        return _call(fetch)
    except ToolError as exc:
        raise ToolError(
            f"Could not fetch post @{post_number} in class {network_id}: {exc}. "
            "Call get_feed or search_posts to find valid post numbers."
        ) from exc


def http_session():
    """The authenticated requests session, for downloading attachments."""
    return _piazza_instance()._rpc_api.session
