# kaynakplanlar

`kaynakplanlar` schedules build tasks into a single time window. Give it a
window (say, one night on the CI machine) and a list of jobs with durations,
priorities, dependencies and repeat patterns; it greedily lays the jobs onto
the one machine and tells you exactly what had to give way: which runs were
pushed, which were dropped because no slot fit, and which were blocked because
a dependency never ran.

It answers the question "if I queue all of this, what actually runs, when, and
what got in the way?" before you burn the window. It is a single
standard-library-only Python package with no configuration, no daemon and no
state: one JSON plan in, one schedule out.

## A real example

Save this as `plan.json` — four build tasks for a one-day window. `migrate`
must run alone (`exclusive`), `build-core` needs `fetch` to finish first, and
`tests` needs `build-core` and reruns every two hours between 02:00 and 05:00,
at most three times:

```json
{"window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-02T00:00:00+00:00"},
 "jobs": [
   {"id": "build-core", "duration_min": 30, "priority": 1, "deps": ["fetch"]},
   {"id": "fetch", "duration_min": 10, "priority": 1},
   {"id": "migrate", "duration_min": 15, "priority": 0, "exclusive": true},
   {"id": "tests", "duration_min": 45, "priority": 2, "deps": ["build-core"],
    "earliest": "02:00", "latest": "05:00", "repeat": "every 2h", "max_runs": 3}]}
```

Run it:

```
$ python3 -m kaynakplanlar plan.json --load
```

```text
PUSHED id=fetch run=1 from=2026-10-01T00:00:00+00:00 to=2026-10-01T00:15:00+00:00 reason=collision
PUSHED id=build-core run=1 from=2026-10-01T00:00:00+00:00 to=2026-10-01T00:25:00+00:00 reason=dep
DROPPED id=tests run=3 reason=no-slot
start_iso                  end_iso                    id          run#  priority  deps
-------------------------  -------------------------  ----------  ----  --------  ----------
2026-10-01T00:00:00+00:00  2026-10-01T00:15:00+00:00  migrate     1     0         -
2026-10-01T00:15:00+00:00  2026-10-01T00:25:00+00:00  fetch       1     1         -
2026-10-01T00:25:00+00:00  2026-10-01T00:55:00+00:00  build-core  1     1         fetch
2026-10-01T02:00:00+00:00  2026-10-01T02:45:00+00:00  tests       1     2         build-core
2026-10-01T04:00:00+00:00  2026-10-01T04:45:00+00:00  tests       2     2         build-core
jobs=4 runs=5 dropped=1 blocked=0 pushed=2

load (scheduled minutes per hour)
2026-10-01T00:00:00+00:00  55  ████████████████████
2026-10-01T01:00:00+00:00   0
2026-10-01T02:00:00+00:00  45  ████████████████
2026-10-01T03:00:00+00:00   0
2026-10-01T04:00:00+00:00  45  ████████████████
...
busiest hour: 55 min
```

What happened, in the scheduler's own words: priority 0 `migrate` took the
window opening and, being exclusive, took it alone; `fetch` wanted to start at
midnight too and was pushed 15 minutes (`reason=collision`); `build-core` was
held back until its dependency finished (`reason=dep`); `tests` ran at 02:00
and 04:00 inside its daily slot, and its third run — which would have rolled
past the 05:00 cutoff to the next day — had no slot left, so it was dropped
with a warning while the exit code stayed 0.

## Usage

```
python3 -m kaynakplanlar [FILE] [options]
```

Reads the plan from `FILE`, or stdin when `FILE` is `-` (the default).

| Option | Effect |
| --- | --- |
| `--json` | Emit the machine-readable document instead of the table. |
| `--load` | Append a per-hour histogram of scheduled minutes, with `█` bars scaled to the busiest hour. |
| `--bar N` | Width, in cells, of the busiest load bar (default 20). |
| `--strict` | Treat any warning (PUSHED / DROPPED / BLOCKED) as a failure: exit 1. |
| `--version` | Print the version and exit. |

### Input fields

The plan is a JSON object with `window` (`start`/`end`, ISO-8601; a naive
timestamp is read as UTC) and `jobs`. Each job supports:

- `id` — required, unique.
- `duration_min` — required, integer ≥ 1.
- `priority` — lower runs earlier (default 100).
- `deps` — ids of jobs that must finish first. An unknown id, or a dependency
  cycle, is a usage error.
- `exclusive` — the run must not overlap any placement (default false).
- `earliest` / `latest` — `HH:MM` bounds on the daily start time, inclusive.
- `repeat` — `every Nmin`, `every Nh`, `every Nd` or `daily`.
- `max_runs` — cap on repeated runs (default 1; > 1 requires `repeat`).

### Output

`--json` prints `{schedule, warnings, summary}`. Each schedule entry carries
`{id, run, start, end, priority, exclusive, deps, pushed}` sorted by start;
warnings carry `{code, id, run, detail}` with `code` one of `PUSHED`,
`DROPPED`, `BLOCKED`; `summary` counts `jobs`, `runs`, `dropped`, `blocked`,
`pushed`. `--load` adds a `load` array of per-hour `{hour, minutes, bar}` rows.

Warning lines on stderr follow the same vocabulary:
`PUSHED id=… run=… from=… to=… reason=collision|exclusive|dep`,
`DROPPED id=… run=… reason=no-slot`,
`BLOCKED id=… reason=dep-missing:dep-id`. Warnings never change the exit code
on their own.

Exit codes:

- `0` — a schedule was produced (warnings allowed).
- `1` — nothing could be scheduled (every job dropped or blocked), or
  `--strict` was given and at least one warning fired.
- `2` — usage error: unreadable input, malformed JSON, a bad window or job,
  an unknown dependency, a dependency cycle, or a bad flag.

## Tests

```
python3 -m unittest discover -s tests
```

The suite covers window and field validation, priority and tie-break order,
push/drop/block behaviour with all three push reasons, recurrence anchoring
and drift, exclusive placement, the per-hour load histogram and bar scaling,
the JSON document, and every exit-code path of the CLI.

## Design notes

**One machine.** There is a single resource, so no two placements ever
overlap — an `exclusive` job and a plain job share the same no-overlap rule;
`exclusive` only changes *labelling*: when an exclusive job is pushed, the
warning says `reason=exclusive`.

**Greedy order.** Jobs are processed in a topological order of the dependency
graph, and among the jobs whose dependencies are all placed, the one with the
lowest `(priority, id)` goes first. That makes the schedule deterministic —
same input, same output — and gives priority ties a stable, explainable
winner.

**Placement.** A run's *wanted* start is its natural candidate: the window
start for the first run, the recurrence anchor afterwards, rolled forward to
the job's daily `earliest` when the time of day forbids it. If the machine is
busy then, the run is pushed to the end of the latest overlapping placement
and the search repeats; if the dependency bound holds it back, it is pushed to
the dependency's end. `earliest`/`latest` constrain the *start* only and are
re-evaluated every day of the window; a run must end at or before the window
end (the window is half-open, `[start, end)`). A run that cannot fit anywhere
is dropped and the warning says so.

**Dependencies.** A placement starts at or after the end of the latest placed
run of each dependency; for repeated dependencies that is the latest end among
all preceding dependency runs. A dependency that never ran blocks the
dependent (`BLOCKED … reason=dep-missing:…`), and the block propagates
transitively — a dropped job takes its whole downstream with it.

**Recurrence drift.** The next run is anchored at the previous *placed start*
plus the interval, not at the previous end. When the interval is shorter than
the duration — `every 10min` with a 30-minute job — the anchor lands inside
the previous run and the actual start drifts to the previous end, so runs
settle back-to-back; the drift is `duration − interval` per run and is
reported as ordinary collision pushes. This keeps the schedule self-correcting
(a late run delays the next anchor rather than stacking up phantom slots).

**Purity.** Parsing (`model.py`), scheduling (`schedule.py`) and rendering
(`render.py`) are pure and deterministic — no clock reads, no randomness, no
I/O. Only `cli.py` touches streams and exit codes, which is what makes the
scheduler unit-testable end to end.
