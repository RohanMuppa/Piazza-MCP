"""Command-line access to the same Piazza layer the MCP server uses.

For deterministic jobs that need no model in the loop, such as pulling every
file from a class's Resources page into a folder on a schedule:

    piazza classes
    piazza resources list <network_id>
    piazza resources download <network_id> --dest ~/school/ece362
"""

import argparse
import os
import sys
from pathlib import Path

from fastmcp.exceptions import ToolError

from piazza_mcp import client, downloads, formatting


def _classes(args) -> int:
    classes = client.user_classes()
    if not classes:
        print("No enrolled Piazza classes found.")
    for cls in classes:
        print(f"- {formatting.class_label(cls)}")
    return 0


def _resources_list(args) -> int:
    print(formatting.format_resources(client.resources(args.network_id)))
    return 0


def _select(args, entries: list[dict]) -> list[dict] | None:
    """The resources a download run covers, or None after printing an error."""
    if args.resource_ids:
        by_id = {entry.get("id"): entry for entry in entries}
        missing = [rid for rid in args.resource_ids if rid not in by_id]
        if missing:
            print(f"error: unknown resource id(s): {', '.join(missing)}", file=sys.stderr)
            return None
        return [by_id[rid] for rid in args.resource_ids]
    if args.section:
        sections = sorted({formatting.resource_section(entry) for entry in entries})
        if args.section not in sections:
            print(
                f"error: no section {args.section!r}; this class has: "
                f"{', '.join(sections) or '(no resources)'}",
                file=sys.stderr,
            )
            return None
        return [e for e in entries if formatting.resource_section(e) == args.section]
    return entries


def _resources_download(args) -> int:
    # Relative --dest is resolved against the shell's working directory here;
    # the shared code only accepts absolute paths. Validating once up front
    # turns a bad folder into one error instead of one failure per file.
    dest = str(downloads.resolve_dest_dir(str(Path(args.dest).expanduser().resolve())))
    all_entries = client.resources(args.network_id)
    entries = _select(args, all_entries)
    if entries is None:
        return 1
    # Disambiguate against the whole page, not just this selection, so a file
    # keeps the same name whether it is synced alone or with everything.
    renamed = downloads.unique_filenames(all_entries)

    saved = skipped = failed = 0
    for entry in entries:
        title = formatting.resource_title(entry)
        if not downloads.is_file(entry):
            link = (entry.get("content") or "").strip()
            if args.resource_ids:  # asked for by id, so not getting it is a failure
                print(f"Failed {title}: it is a link, not a file: {link}", file=sys.stderr)
                failed += 1
            else:
                print(f"Skipped {title} (link: {link})")
                skipped += 1
            continue
        resource_id = entry.get("id", "")
        try:
            result = downloads.save_resource(
                args.network_id,
                resource_id,
                dest,
                filename=renamed.get(resource_id),
                overwrite=args.overwrite,
                resource=entry,
            )
        except downloads.AlreadyExists as exc:
            print(f"Skipped {title} ({exc.path.name} already exists)")
            skipped += 1
        except Exception as exc:  # one bad file should not stop the sync
            print(f"Failed {title}: {exc}", file=sys.stderr)
            failed += 1
        else:
            print(result.describe())
            saved += 1

    print(f"{saved} saved, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="piazza",
        description="Read Piazza from the terminal. Needs PIAZZA_EMAIL and PIAZZA_PASSWORD.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    classes = commands.add_parser("classes", help="list enrolled classes and their network ids")
    classes.set_defaults(handler=_classes)

    resources = commands.add_parser("resources", help="a class's Resources page")
    resource_commands = resources.add_subparsers(dest="resource_command", required=True)

    listing = resource_commands.add_parser("list", help="list files and links")
    listing.add_argument("network_id", help="class id from `piazza classes`")
    listing.set_defaults(handler=_resources_list)

    download = resource_commands.add_parser(
        "download",
        help="save files to a folder (all of them unless ids or --section are given)",
        description=(
            "Save Resources files into --dest. Files that already exist are "
            "skipped unless --overwrite is given, so re-running syncs new files only. "
            "Resources whose titles would save to the same filename get their "
            "resource id appended."
        ),
    )
    download.add_argument("network_id", help="class id from `piazza classes`")
    selection = download.add_mutually_exclusive_group()
    selection.add_argument("resource_ids", nargs="*", default=[], help="only these resource ids")
    selection.add_argument("--section", help="only this section, as shown by `resources list`")
    download.add_argument("--dest", required=True, help="folder to save into (created if missing)")
    download.add_argument("--overwrite", action="store_true", help="replace existing files")
    download.set_defaults(handler=_resources_download)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except BrokenPipeError:
        raise  # handled in run(), which owns stdout
    except (ToolError, OSError) as exc:  # OSError covers requests' network errors
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def run() -> int:
    """Console-script entry point."""
    try:
        return main()
    except BrokenPipeError:
        # Output was piped into something that closed early (e.g. `| head`).
        # Point stdout at devnull so the interpreter's final flush stays quiet.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


if __name__ == "__main__":
    sys.exit(run())
