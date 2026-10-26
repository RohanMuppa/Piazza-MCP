"""The `piazza` command with the Piazza session mocked. No network."""

import pytest
from fakes import NID, FakeResponse

from piazza_mcp import cli, downloads
from piazza_mcp import client as piazza_client


def test_classes(monkeypatch, capsys):
    monkeypatch.setattr(
        piazza_client,
        "user_classes",
        lambda: [{"name": "WL-ECE 36200", "term": "Fall 2026", "nid": NID}],
    )
    assert cli.main(["classes"]) == 0
    assert f"WL-ECE 36200 (Fall 2026) | network_id: {NID}" in capsys.readouterr().out


def test_resources_list(fake_session, capsys):
    assert cli.main(["resources", "list", NID]) == 0
    out = capsys.readouterr().out
    assert "## homework_solutions" in out
    assert "**riscv.pdf** [file] resource_id: mvieovrvaql6o1" in out


def test_download_all_then_rerun_skips_existing(fake_session, tmp_path, capsys):
    assert cli.main(["resources", "download", NID, "--dest", str(tmp_path)]) == 0
    first = capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Homework_1_PDF.pdf",
        "Homework_1_Solutions_PDF.pdf",
        "riscv.pdf",
    ]
    assert "Skipped Course site ]; (link: https://engineering.purdue.edu/ece362/)" in first
    assert first.strip().endswith("3 saved, 1 skipped, 0 failed")

    assert cli.main(["resources", "download", NID, "--dest", str(tmp_path)]) == 0
    second = capsys.readouterr().out
    assert "Skipped riscv.pdf (riscv.pdf already exists)" in second
    assert second.strip().endswith("0 saved, 4 skipped, 0 failed")


def test_download_overwrite_replaces(fake_session, tmp_path, capsys):
    (tmp_path / "riscv.pdf").write_bytes(b"old")
    args = ["resources", "download", NID, "mvieovrvaql6o1", "--dest", str(tmp_path)]
    assert cli.main([*args, "--overwrite"]) == 0
    assert (tmp_path / "riscv.pdf").read_bytes() == b"%PDF-1.4 body"


def test_download_section(fake_session, tmp_path):
    assert cli.main(
        ["resources", "download", NID, "--dest", str(tmp_path), "--section", "homework"]
    ) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["Homework_1_PDF.pdf"]


def test_download_section_general_includes_unsectioned(fake_session, tmp_path, monkeypatch):
    entries = [
        {"id": "aaa1", "subject": "Syllabus", "config": {"resource_type": "file"}},
        {"id": "bbb2", "subject": "HW", "config": {"resource_type": "file", "section": "homework"}},
    ]
    monkeypatch.setattr(piazza_client, "resources", lambda nid: entries)
    assert cli.main(
        ["resources", "download", NID, "--dest", str(tmp_path), "--section", "general"]
    ) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["Syllabus.pdf"]


def test_download_unknown_section_is_an_error(fake_session, tmp_path, capsys):
    code = cli.main(["resources", "download", NID, "--dest", str(tmp_path), "--section", "hw"])
    assert code == 1
    assert "this class has: general, homework, homework_solutions" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_ids_and_section_together_are_rejected(fake_session, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["resources", "download", NID, "mvieovrvaql6o1", "--section", "general",
             "--dest", str(tmp_path)]
        )
    assert exc.value.code == 2


def test_colliding_titles_get_distinct_stable_names(fake_session, tmp_path, monkeypatch, capsys):
    entries = [
        {"id": "aaa1", "subject": "Notes", "config": {"resource_type": "file"}},
        {"id": "bbb2", "subject": "notes", "config": {"resource_type": "file"}},
        {"id": "ccc3", "subject": "Notes.pdf", "config": {"resource_type": "file"}},
        {"id": "ddd4", "subject": "Other", "config": {"resource_type": "file"}},
    ]
    monkeypatch.setattr(piazza_client, "resources", lambda nid: entries)
    assert cli.main(["resources", "download", NID, "--dest", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Notes-aaa1.pdf",
        "Notes-ccc3.pdf",
        "Other.pdf",
        "notes-bbb2.pdf",
    ]
    capsys.readouterr()
    # Syncing one colliding file alone uses the same name, so it is recognized.
    assert cli.main(["resources", "download", NID, "bbb2", "--dest", str(tmp_path)]) == 0
    assert "Skipped notes (notes-bbb2.pdf already exists)" in capsys.readouterr().out


def test_named_link_is_a_failure(fake_session, tmp_path, capsys):
    code = cli.main(["resources", "download", NID, "lnk0000000001", "--dest", str(tmp_path)])
    assert code == 1
    assert "is a link, not a file" in capsys.readouterr().err


def test_dest_outside_download_root_fails_once(fake_session, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PIAZZA_DOWNLOAD_ROOT", str(tmp_path / "root"))
    code = cli.main(["resources", "download", NID, "--dest", str(tmp_path / "elsewhere")])
    err = capsys.readouterr().err
    assert code == 1
    assert err.count("outside PIAZZA_DOWNLOAD_ROOT") == 1
    assert not (tmp_path / "elsewhere").exists()


def test_dest_that_is_a_file_fails_once(fake_session, tmp_path, capsys):
    target = tmp_path / "afile"
    target.write_text("x")
    assert cli.main(["resources", "download", NID, "--dest", str(target)]) == 1
    assert capsys.readouterr().err.count("is not a folder") == 1


def test_mid_stream_error_counts_as_failure_and_continues(fake_session, tmp_path, capsys):
    fake_session.queued_downloads = [FakeResponse(chunks=[b"part", ConnectionError("reset")])]
    code = cli.main(["resources", "download", NID, "--dest", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "Failed Homework_1_PDF" in captured.err
    assert captured.out.strip().endswith("2 saved, 1 skipped, 1 failed")
    assert not list(tmp_path.glob(".piazza-*"))


def test_unexpected_per_file_error_does_not_stop_sync(fake_session, tmp_path, monkeypatch, capsys):
    real_save = downloads.save_resource

    def flaky_save(network_id, resource_id, *args, **kwargs):
        if resource_id == "mtgo11puylh1cd":
            raise PermissionError("denied")
        return real_save(network_id, resource_id, *args, **kwargs)

    monkeypatch.setattr(downloads, "save_resource", flaky_save)
    assert cli.main(["resources", "download", NID, "--dest", str(tmp_path)]) == 1
    assert capsys.readouterr().out.strip().endswith("2 saved, 1 skipped, 1 failed")


def test_network_error_is_a_clean_error(monkeypatch, capsys):
    import requests

    def offline():
        raise requests.ConnectionError("DNS lookup failed")

    monkeypatch.setattr(piazza_client, "user_classes", offline)
    assert cli.main(["classes"]) == 1
    assert "error: DNS lookup failed" in capsys.readouterr().err


def test_login_network_error_becomes_tool_error(monkeypatch):
    import requests
    from fastmcp.exceptions import ToolError

    monkeypatch.setenv("PIAZZA_EMAIL", "a@b.edu")
    monkeypatch.setenv("PIAZZA_PASSWORD", "x")

    def boom(self, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(piazza_client.Piazza, "user_login", boom)
    piazza_client.reset()
    with pytest.raises(ToolError, match="Could not reach Piazza to log in"):
        piazza_client.user_classes()
    piazza_client.reset()


def test_download_relative_dest_uses_cwd(fake_session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["resources", "download", NID, "mvieovrvaql6o1", "--dest", "out"]) == 0
    assert (tmp_path / "out" / "riscv.pdf").exists()


def test_download_unknown_id(fake_session, tmp_path, capsys):
    code = cli.main(["resources", "download", NID, "nope123", "--dest", str(tmp_path)])
    assert code == 1
    assert "unknown resource id(s): nope123" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_download_failure_continues_and_exits_nonzero(fake_session, tmp_path, capsys):
    # First file hits two refusals (initial try + re-login retry); the rest succeed.
    fake_session.queued_downloads = [FakeResponse(status_code=403), FakeResponse(status_code=403)]
    code = cli.main(["resources", "download", NID, "--dest", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "Failed Homework_1_PDF" in captured.err
    assert "HTTP 403" in captured.err
    assert captured.out.strip().endswith("2 saved, 1 skipped, 1 failed")


def test_missing_credentials_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.delenv("PIAZZA_EMAIL", raising=False)
    monkeypatch.delenv("PIAZZA_PASSWORD", raising=False)
    piazza_client.reset()
    assert cli.main(["classes"]) == 1
    assert "error: PIAZZA_EMAIL" in capsys.readouterr().err


def test_requires_a_command():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
