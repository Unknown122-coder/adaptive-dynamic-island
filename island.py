"""``island`` -- command line client for the Live Activity API.

Examples::

    island push build "docker build · api" --progress 0.4 --detail "step 7/14"
    island done build --status ok --detail "built in 42s"
    island run --id tests --title "pytest" -- pytest -q
    island list
    island ping

``run`` is the one most people will use: it wraps any command, shows it on
the island while it runs, and reports the exit status when it finishes,
without the wrapped tool knowing anything about the island.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ipc.client import ActivityError, IslandClient


def _client(args: argparse.Namespace) -> IslandClient:
    return IslandClient(timeout=args.timeout)


def cmd_ping(args: argparse.Namespace) -> int:
    with _client(args) as client:
        response = client.ping()
    print(f"island is running on port {response.get('port')}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        ids = client.list()
    if not ids:
        print("no live activities")
        return 0
    for activity_id in ids:
        print(activity_id)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    fields = {}
    if args.detail:
        fields["detail"] = args.detail
    if args.progress is not None:
        fields["progress"] = args.progress
    if args.glyph:
        fields["glyph"] = args.glyph
    if args.urgency:
        fields["urgency"] = args.urgency
    if args.ttl is not None:
        fields["ttl"] = args.ttl
    with _client(args) as client:
        client.push(args.id, args.title, **fields)
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    fields = {}
    if args.detail:
        fields["detail"] = args.detail
    if args.title:
        fields["title"] = args.title
    with _client(args) as client:
        client.done(args.id, args.status, **fields)
    return 0


def cmd_retract(args: argparse.Namespace) -> int:
    with _client(args) as client:
        client.retract(args.id)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a command and mirror its lifetime onto the island."""
    if not args.command:
        print("island run: no command given after --", file=sys.stderr)
        return 2

    activity_id = args.id or Path(args.command[0]).stem
    title = args.title or " ".join(args.command[:3])
    started = time.monotonic()

    client = IslandClient(timeout=args.timeout)
    reachable = True
    try:
        client.push(
            activity_id,
            title,
            progress=-1.0,
            glyph=args.glyph or "pulse",
            detail="running",
            ttl=args.ttl,
        )
    except ActivityError as exc:
        # Never fail the user's command because the island is absent.
        reachable = False
        if args.verbose:
            print(f"island: {exc}", file=sys.stderr)

    try:
        completed = subprocess.run(args.command, check=False)
        code = completed.returncode
    except KeyboardInterrupt:
        code = 130
    except OSError as exc:
        print(f"island run: {exc}", file=sys.stderr)
        code = 127

    if reachable:
        elapsed = time.monotonic() - started
        status = "ok" if code == 0 else ("cancelled" if code == 130 else "fail")
        detail = f"exit {code} · {elapsed:.1f}s"
        with contextlib.suppress(ActivityError):
            client.done(activity_id, status, title=title, detail=detail)
    client.close()
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="island",
        description="Push live activities to the Adaptive Dynamic Island.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="socket timeout in seconds (default: 2)",
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command_name", required=True)

    p_ping = sub.add_parser("ping", help="check that the island is running")
    p_ping.set_defaults(func=cmd_ping)

    p_list = sub.add_parser("list", help="list live activity ids")
    p_list.set_defaults(func=cmd_list)

    p_push = sub.add_parser("push", help="create or update an activity")
    p_push.add_argument("id", help="stable id, e.g. build")
    p_push.add_argument("title", help="headline shown on the island")
    p_push.add_argument("--detail", help="secondary line")
    p_push.add_argument(
        "--progress",
        type=float,
        help="0.0-1.0, or a negative value for an indeterminate bar",
    )
    p_push.add_argument("--glyph", help="icon name, e.g. cube, flask, download")
    p_push.add_argument(
        "--urgency",
        choices=["neutral", "info", "success", "warning", "critical"],
    )
    p_push.add_argument(
        "--ttl",
        type=float,
        help="seconds before the island drops it if not updated",
    )
    p_push.set_defaults(func=cmd_push)

    p_done = sub.add_parser("done", help="mark an activity finished")
    p_done.add_argument("id")
    p_done.add_argument(
        "--status", choices=["ok", "fail", "cancelled"], default="ok"
    )
    p_done.add_argument("--title")
    p_done.add_argument("--detail")
    p_done.set_defaults(func=cmd_done)

    p_retract = sub.add_parser("retract", help="remove an activity immediately")
    p_retract.add_argument("id")
    p_retract.set_defaults(func=cmd_retract)

    p_run = sub.add_parser(
        "run",
        help="run a command and show it on the island while it runs",
    )
    p_run.add_argument("--id", help="activity id (default: the program name)")
    p_run.add_argument("--title", help="headline (default: the command line)")
    p_run.add_argument("--glyph")
    p_run.add_argument("--ttl", type=float, default=900.0)
    p_run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="the command to run, after --",
    )
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # argparse keeps the "--" separator in REMAINDER; drop it.
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        return int(args.func(args))
    except ActivityError as exc:
        print(f"island: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
