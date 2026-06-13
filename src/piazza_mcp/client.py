"""Authenticated access to Piazza. All piazza-api usage lives here.

Piazza has no official API; piazza-api drives the internal JSON-RPC
endpoint with a cookie session. This module owns the lazy login, a short
TTL cache so tools do not hammer the endpoint, and one re-login retry
for expired sessions.
"""

import os
import threading
import time

from fastmcp.exceptions import ToolError
from piazza_api import Piazza
from piazza_api.exceptions import AuthenticationError, RequestError
from piazza_api.network import FolderFilter, UnreadFilter

CACHE_TTL_SECONDS = 60
FEED_FETCH_LIMIT = 200

_lock = threading.RLock()
_piazza = None
_cache = {}


def reset():
    """Drop the session and caches. Used on auth failure and in tests."""
    global _piazza
    with _lock:
        _piazza = None
        _cache.clear()


def _piazza_instance():
    global _piazza
    with _lock:
        if _piazza is None:
            email = os.environ.get("PIAZZA_EMAIL")
            password = os.environ.get("PIAZZA_PASSWORD")
            if not email or not password:
                raise ToolError(
                    "PIAZZA_EMAIL and PIAZZA_PASSWORD environment variables must be "
                    "set to your Piazza login credentials."
                )
            piazza = Piazza()
            try:
                piazza.user_login(email=email, password=password)
            except AuthenticationError as exc:
                raise ToolError(f"Piazza login failed: {exc}") from exc
            _piazza = piazza
        return _piazza


def _call(fn):
    """Run fn against a live session; re-login once if the session expired."""
    try:
        return fn(_piazza_instance())
    except ToolError:
        raise
    except (RequestError, AuthenticationError):
        reset()
        try:
            return fn(_piazza_instance())
        except ToolError:
            raise
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


def folders(network_id):
    """Folder names configured for one class."""
    status = user_status()
    for network in status.get("networks", []):
        if network.get("id") == network_id:
            return network.get("folders") or []
    raise ToolError(
        f"Class {network_id} was not found in your enrolled classes. "
        "Call list_classes to see valid network ids."
    )


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
