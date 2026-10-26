"""Render Piazza JSON structures into compact, scannable markdown."""

from datetime import datetime, timezone

from markdownify import markdownify

SNIPPET_MAX_CHARS = 160


def html_to_md(html: str | None) -> str:
    """Convert Piazza HTML content to markdown, collapsing blank-line runs."""
    if not html:
        return ""
    lines = [line.rstrip() for line in markdownify(html, heading_style="ATX").splitlines()]
    out: list[str] = []
    previous_blank = False
    for line in lines:
        if line:
            out.append(line)
            previous_blank = False
        elif not previous_blank:
            out.append("")
            previous_blank = True
    return "\n".join(out).strip()


def parse_time(value: str | None) -> datetime | None:
    """Parse a Piazza ISO timestamp such as 2026-06-10T12:34:56Z."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _item_status(item: dict) -> str:
    bits = []
    if item.get("no_answer"):
        bits.append("UNANSWERED")
    if item.get("has_i"):
        bits.append("instructor answered")
    if item.get("has_s"):
        bits.append("student answered")
    return ", ".join(bits)


def format_feed_item(item: dict) -> str:
    """One feed or search result as a two-line markdown bullet."""
    number = item.get("nr", "?")
    subject = (item.get("subject") or "(no subject)").strip()
    parts = [f"- **@{number}** [{item.get('type', 'post')}] {subject}"]
    details = []
    status = _item_status(item)
    if status:
        details.append(status)
    folders = item.get("folders") or []
    if folders:
        details.append("folders: " + ", ".join(folders))
    modified = item.get("modified") or item.get("updated")
    if modified:
        details.append(f"updated {modified}")
    if details:
        parts.append("  " + " | ".join(details))
    snippet = " ".join((item.get("content_snipet") or "").split())
    if snippet:
        parts.append("  > " + snippet[:SNIPPET_MAX_CHARS])
    return "\n".join(parts)


def format_feed_items(items: list[dict], has_more: bool = False) -> str:
    if not items:
        return "No posts found."
    lines = [format_feed_item(item) for item in items]
    if has_more:
        lines.append("")
        lines.append("(more posts available; call again with a higher offset)")
    return "\n".join(lines)


def _format_answer(child: dict) -> str:
    label = "Instructor Answer" if child.get("type") == "i_answer" else "Student Answer"
    history = child.get("history") or [{}]
    body = html_to_md(history[0].get("content")) or "(empty)"
    endorsements = child.get("tag_endorse") or child.get("tag_good") or []
    suffix = f" (endorsed by {len(endorsements)})" if endorsements else ""
    return f"## {label}{suffix}\n\n{body}"


def _format_followup(child: dict, depth: int = 0) -> str:
    indent = "  " * depth
    text = html_to_md(child.get("subject")) or "(empty)"
    marker = "unresolved" if child.get("no_answer") else "resolved"
    lines = [f"{indent}- followup ({marker}): {text}"]
    for reply in child.get("children") or []:
        reply_text = html_to_md(reply.get("subject")) or "(empty)"
        lines.append(f"{indent}  - reply: {reply_text}")
    return "\n".join(lines)


def format_post(post: dict) -> str:
    """Full thread: question, answers, followups."""
    history = post.get("history") or [{}]
    latest = history[0]
    subject = (latest.get("subject") or "(no subject)").strip()
    lines = [f"# @{post.get('nr', '?')} {subject}".rstrip()]

    meta = [f"type: {post.get('type', 'post')}"]
    folders = post.get("folders") or []
    if folders:
        meta.append("folders: " + ", ".join(folders))
    created = post.get("created")
    if created:
        meta.append(f"created: {created}")
    if latest.get("anon") and latest.get("anon") != "no":
        meta.append("posted anonymously")
    lines.append(" | ".join(meta))
    lines.append("")
    lines.append(html_to_md(latest.get("content")) or "(empty)")

    followups = []
    for child in post.get("children") or []:
        if child.get("type") in ("i_answer", "s_answer"):
            lines.append("")
            lines.append(_format_answer(child))
        else:
            followups.append(child)

    if followups:
        lines.append("")
        lines.append("## Followups")
        lines.append("")
        for child in followups:
            lines.append(_format_followup(child))
    return "\n".join(lines)


def class_label(cls: dict) -> str:
    """One class as "Name (Term) | network_id: abc123"."""
    name = cls.get("name") or cls.get("num") or "Unknown class"
    term = cls.get("term")
    network_id = cls.get("nid") or cls.get("id") or "?"
    label = f"{name} ({term})" if term else str(name)
    return f"{label} | network_id: {network_id}"


def resource_title(resource: dict) -> str:
    """Display name of a resource: its subject, falling back to its content."""
    return (resource.get("subject") or resource.get("content") or "(untitled)").strip()


def resource_section(resource: dict) -> str:
    """Section a resource is listed under; unsectioned ones count as general."""
    return (resource.get("config") or {}).get("section") or "general"


def format_resources(resources: list[dict]) -> str:
    """Class Resources grouped by section, one bullet per file or link."""
    if not resources:
        return "This class has no Resources."
    sections: dict[str, list[dict]] = {}
    for resource in resources:
        section = resource_section(resource)
        sections.setdefault(section, []).append(resource)
    lines = []
    for section, items in sections.items():
        if lines:
            lines.append("")
        lines.append(f"## {section}")
        for resource in items:
            config = resource.get("config") or {}
            kind = config.get("resource_type") or "file"
            line = f"- **{resource_title(resource)}** [{kind}] resource_id: {resource.get('id', '?')}"
            updated = resource.get("updated") or resource.get("created")
            if updated:
                line += f" | updated {updated}"
            if kind == "link" and resource.get("content"):
                line += f"\n  {resource['content'].strip()}"
            lines.append(line)
    return "\n".join(lines)


def format_digest(sections: list[tuple[str, list[dict], bool]], hours: int) -> str:
    """Cross-class digest. sections is (class_label, items, truncated)."""
    lines = [f"# Piazza digest: last {hours} hours"]
    any_activity = False
    for label, items, truncated in sections:
        lines.append("")
        lines.append(f"## {label}")
        if not items:
            lines.append("No new activity.")
            continue
        any_activity = True
        for item in items:
            lines.append(format_feed_item(item))
        if truncated:
            lines.append(f"(only the {len(items)} most recent items shown for this class)")
    if not any_activity:
        lines.append("")
        lines.append("No new activity in any class.")
    return "\n".join(lines)
