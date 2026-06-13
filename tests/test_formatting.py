from datetime import datetime, timezone

from piazza_mcp import formatting


def test_html_to_md_converts_tags():
    md = formatting.html_to_md("<p>Hello <b>world</b></p><p></p><p>bye</p>")
    assert "**world**" in md
    assert "\n\n\n" not in md


def test_html_to_md_handles_empty():
    assert formatting.html_to_md(None) == ""
    assert formatting.html_to_md("") == ""


def test_parse_time_z_suffix():
    parsed = formatting.parse_time("2026-06-09T18:30:00Z")
    assert parsed == datetime(2026, 6, 9, 18, 30, tzinfo=timezone.utc)


def test_parse_time_invalid():
    assert formatting.parse_time("not a date") is None
    assert formatting.parse_time(None) is None


def test_format_feed_item(feed_item_fixture):
    text = formatting.format_feed_item(feed_item_fixture)
    assert "@42" in text
    assert "When is HW1 due?" in text
    assert "instructor answered" in text
    assert "hw1" in text
    assert "Is it due Friday?" in text


def test_format_feed_item_unanswered():
    text = formatting.format_feed_item({"nr": 7, "subject": "Help", "no_answer": 1})
    assert "UNANSWERED" in text


def test_format_feed_items_empty():
    assert formatting.format_feed_items([]) == "No posts found."


def test_format_feed_items_has_more(feed_item_fixture):
    text = formatting.format_feed_items([feed_item_fixture], has_more=True)
    assert "more posts available" in text


def test_format_post(post_fixture):
    text = formatting.format_post(post_fixture)
    assert text.startswith("# @42 When is HW1 due?")
    assert "posted anonymously" in text
    assert "## Instructor Answer (endorsed by 2)" in text
    assert "Friday at 11:59pm" in text
    assert "## Student Answer" in text
    assert "## Followups" in text
    assert "followup (unresolved): Will there be a late policy?" in text
    assert "reply: Yes, two late days." in text
    assert "/redirect/s3?bucket=uploads&prefix=hw1.pdf" in text


def test_format_digest_groups_by_class(feed_item_fixture):
    text = formatting.format_digest(
        [("CS 252 (Spring 2026)", [feed_item_fixture], False), ("MA 351", [], False)],
        24,
    )
    assert "# Piazza digest: last 24 hours" in text
    assert "## CS 252 (Spring 2026)" in text
    assert "@42" in text
    assert "## MA 351" in text
    assert "No new activity." in text


def test_format_digest_truncation_note(feed_item_fixture):
    text = formatting.format_digest([("CS 252", [feed_item_fixture], True)], 24)
    assert "most recent items shown" in text


def test_format_digest_all_quiet():
    text = formatting.format_digest([("CS 252", [], False)], 12)
    assert "No new activity in any class." in text
