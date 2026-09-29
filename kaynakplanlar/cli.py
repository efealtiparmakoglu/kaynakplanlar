"""Command line entry point.

Exit codes
----------
0  a schedule was produced (warnings allowed unless ``--strict``)
1  every job was dropped or blocked, or ``--strict`` and a warning fired
2  usage error: unreadable input, bad window/job, dependency cycle, bad flags
"""

import argparse
import json
import sys
from typing import List, Optional, Sequence, TextIO

from .model import UsageError, parse_plan
from .render import (
    DEFAULT_BAR_WIDTH,
    build_json,
    dumps_json,
    render_load,
    render_summary,
    render_table,
    render_warnings,
)
from .schedule import schedule_plan

PROG = "kaynakplanlar"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Schedule build tasks into a time window, honouring priority, "
                    "dependencies and recurrence.",
        epilog="Exit codes: 0 ok, 1 nothing scheduled (or --strict with warnings), 2 usage error.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="path to the plan JSON file, or '-' for stdin (default: stdin)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit the machine-readable document instead of a table")
    parser.add_argument("--load", action="store_true",
                        help="append a per-hour histogram of scheduled minutes")
    parser.add_argument("--bar", type=int, default=DEFAULT_BAR_WIDTH, metavar="N",
                        help="width of the busiest load bar in cells (default: %d)"
                             % DEFAULT_BAR_WIDTH)
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when any warning was emitted")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _read_input(source: str, stdin: TextIO) -> str:
    if source == "-":
        return stdin.read()
    try:
        with open(source, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise UsageError("cannot read %s: %s" % (source, exc.strerror or exc))


def _load_document(text: str, source: str) -> object:
    if not text.strip():
        raise UsageError("%s is empty" % ("stdin" if source == "-" else source))
    try:
        return json.loads(text)
    except ValueError as exc:
        raise UsageError("%s is not valid JSON: %s"
                         % ("stdin" if source == "-" else source, exc))


def _version() -> str:
    from . import __version__

    return "%s %s" % (PROG, __version__)


def main(argv: Optional[Sequence[str]] = None,
         stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None,
         stdin: Optional[TextIO] = None) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    inp = sys.stdin if stdin is None else stdin

    parser = build_parser()
    argv_list = list(sys.argv[1:] if argv is None else argv)
    # argparse prints usage/errors to the process streams; swap them in so the
    # injected streams receive everything, then restore before doing real work.
    real_out, real_err = sys.stdout, sys.stderr
    swapped_out, swapped_err = out is not real_out, err is not real_err
    try:
        if swapped_out:
            sys.stdout = out
        if swapped_err:
            sys.stderr = err
        try:
            args = parser.parse_args(argv_list)
        finally:
            if swapped_out:
                sys.stdout = real_out
            if swapped_err:
                sys.stderr = real_err
    except SystemExit as exc:  # --help exits 0, argparse usage errors exit 2
        code = exc.code
        return code if isinstance(code, int) else EXIT_USAGE

    if args.version:
        out.write(_version() + "\n")
        return EXIT_OK

    if args.bar < 1:
        err.write("%s: error: --bar must be >= 1, got %d\n" % (PROG, args.bar))
        return EXIT_USAGE

    try:
        document = _load_document(_read_input(args.input, inp), args.input)
        plan = parse_plan(document)
        result = schedule_plan(plan)
    except UsageError as exc:
        err.write("%s: error: %s\n" % (PROG, exc))
        return EXIT_USAGE

    if args.as_json:
        payload = build_json(
            result,
            window=plan.window if args.load else None,
            placements=result.schedule if args.load else None,
            bar_width=args.bar,
        )
        out.write(dumps_json(payload))
    else:
        out.write(render_table(result.schedule) + "\n")
        out.write(render_summary(result.summary()) + "\n")
        if args.load:
            out.write("\n" + render_load(plan.window, result.schedule, args.bar) + "\n")

    for line in render_warnings(result.warnings):
        err.write(line + "\n")

    if plan.jobs and not result.schedule:
        return EXIT_FAILED
    if args.strict and result.warnings:
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
