"""Input parsing and validation for kaynakplanlar.

This module is *pure*: it never prints, never reads files and never exits.
Every problem a user can fix in their JSON raises :class:`UsageError`, which
the CLI turns into exit code 2.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "UsageError",
    "Job",
    "Window",
    "Plan",
    "parse_plan",
    "parse_window",
    "parse_jobs",
]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# Documented forms are "every Nmin", "daily" and "every Nd". Hour units and
# spelled-out unit names are accepted too so that "every 2h" also works.
_REPEAT_RE = re.compile(
    r"^every\s+(\d+)\s*(min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$"
)

MINUTES_PER_DAY = 24 * 60
MIDNIGHT = 0
END_OF_DAY = MINUTES_PER_DAY - 1  # 23:59
UTC = timezone.utc


class UsageError(Exception):
    """Any input problem the user is expected to fix."""


# --------------------------------------------------------------------------
# scalar helpers
# --------------------------------------------------------------------------


def _as_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UsageError("%s must be a JSON object, got %s" % (what, _typename(value)))
    return value


def _typename(value: Any) -> str:
    return {
        dict: "object",
        list: "array",
        str: "string",
        bool: "boolean",
        int: "integer",
        float: "number",
        type(None): "null",
    }.get(type(value), type(value).__name__)


def _require_int(value: Any, where: str, minimum: Optional[int] = None) -> int:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError("%s must be an integer, got %s" % (where, _typename(value)))
    if minimum is not None and value < minimum:
        raise UsageError("%s must be >= %d, got %d" % (where, minimum, value))
    return value


def parse_clock(text: Any, where: str) -> int:
    """Parse ``"HH:MM"`` into minutes after midnight."""
    if not isinstance(text, str):
        raise UsageError("%s must be an \"HH:MM\" string, got %s" % (where, _typename(text)))
    match = _TIME_RE.match(text.strip())
    if not match:
        raise UsageError('%s must look like "HH:MM", got %r' % (where, text))
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise UsageError("%s is not a real time of day: %r" % (where, text))
    return hours * 60 + minutes


def parse_iso(text: Any, where: str) -> datetime:
    """Parse an ISO-8601 datetime. A naive value is interpreted as UTC."""
    if not isinstance(text, str):
        raise UsageError("%s must be an ISO-8601 string, got %s" % (where, _typename(text)))
    raw = text.strip()
    if not raw:
        raise UsageError("%s must not be empty" % where)
    # ``datetime.fromisoformat`` only learned to read a trailing "Z" in 3.11.
    normalised = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        value = datetime.fromisoformat(normalised)
    except ValueError:
        raise UsageError("%s is not a valid ISO-8601 datetime: %r" % (where, text))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_repeat(value: Any, where: str) -> Tuple[Optional[timedelta], str]:
    """Return ``(interval, canonical_repeat_text)`` for a repeat pattern.

    An absent/empty pattern yields ``(None, "")``, meaning "run once".
    """
    if value is None:
        return None, ""
    if not isinstance(value, str):
        raise UsageError('%s must be a string ("every Nmin", "every Nh", "daily", "every Nd"), '
                         'got %s' % (where, _typename(value)))
    raw = value.strip().lower()
    if not raw:
        return None, ""
    if raw == "daily":
        return timedelta(days=1), "daily"
    match = _REPEAT_RE.match(raw)
    if not match:
        raise UsageError('%s must be "every Nmin", "every Nh", "daily" or "every Nd", got %r'
                         % (where, value))
    amount = int(match.group(1))
    if amount < 1:
        raise UsageError("%s interval must be >= 1, got %d" % (where, amount))
    unit = match.group(2)
    if unit.startswith("min"):
        return timedelta(minutes=amount), "every %dmin" % amount
    if unit.startswith("h"):
        return timedelta(hours=amount), "every %dh" % amount
    return timedelta(days=amount), "every %dd" % amount


# --------------------------------------------------------------------------
# dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """The half-open interval ``[start, end)`` placements must live inside."""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


@dataclass(frozen=True)
class Job:
    """One validated unit of work."""

    id: str
    duration: timedelta
    duration_min: int
    priority: int
    deps: Tuple[str, ...]
    exclusive: bool
    earliest: int
    latest: int
    repeat: str
    interval: Optional[timedelta]
    max_runs: int

    @property
    def has_daily_window(self) -> bool:
        return self.earliest != MIDNIGHT or self.latest != END_OF_DAY


@dataclass(frozen=True)
class Plan:
    """A window plus every job that references it."""

    window: Window
    jobs: Tuple[Job, ...]

    def job(self, job_id: str) -> Job:
        for candidate in self.jobs:
            if candidate.id == job_id:
                return candidate
        raise KeyError(job_id)

    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(j.id for j in self.jobs)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_window(raw: Any) -> Window:
    data = _as_mapping(raw, '"window"')
    for key in ("start", "end"):
        if key not in data:
            raise UsageError('"window" is missing required key %r' % key)
    start = parse_iso(data["start"], '"window.start"')
    end = parse_iso(data["end"], '"window.end"')
    if end <= start:
        raise UsageError('"window.end" (%s) must be after "window.start" (%s)'
                         % (end.isoformat(), start.isoformat()))
    return Window(start=start, end=end)


def _parse_job(raw: Any, index: int) -> Job:
    if not isinstance(raw, Mapping):
        raise UsageError("jobs[%d] must be an object, got %s" % (index, _typename(raw)))
    job_id = raw.get("id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise UsageError('jobs[%d]: "id" is required and must be a non-empty string' % index)
    job_id = job_id.strip()
    where = "job %r" % job_id

    if "duration_min" not in raw:
        raise UsageError("%s: \"duration_min\" is required" % where)
    duration_min = _require_int(raw["duration_min"], '%s: "duration_min"' % where, minimum=1)

    priority = 100
    if raw.get("priority") is not None:
        priority = _require_int(raw["priority"], '%s: "priority"' % where)

    deps_raw = raw.get("deps") or []
    if not isinstance(deps_raw, (list, tuple)):
        raise UsageError('%s: "deps" must be an array of job ids, got %s'
                         % (where, _typename(deps_raw)))
    deps: List[str] = []
    for dep in deps_raw:
        if not isinstance(dep, str) or not dep.strip():
            raise UsageError('%s: "deps" must contain non-empty job id strings' % where)
        if dep.strip() not in deps:
            deps.append(dep.strip())
    if job_id in deps:
        raise UsageError("%s: dependency cycle detected: %s -> %s" % (where, job_id, job_id))

    exclusive = raw.get("exclusive", False)
    if not isinstance(exclusive, bool):
        raise UsageError('%s: "exclusive" must be a boolean, got %s' % (where, _typename(exclusive)))

    earliest = MIDNIGHT
    if raw.get("earliest") is not None:
        earliest = parse_clock(raw["earliest"], '%s: "earliest"' % where)
    latest = END_OF_DAY
    if raw.get("latest") is not None:
        latest = parse_clock(raw["latest"], '%s: "latest"' % where)
    if earliest > latest:
        raise UsageError("%s: \"earliest\" (%02d:%02d) must not be later than \"latest\" (%02d:%02d)"
                         % (where, earliest // 60, earliest % 60, latest // 60, latest % 60))

    interval, repeat_text = _parse_repeat(raw.get("repeat"), '%s: "repeat"' % where)

    max_runs = 1
    if raw.get("max_runs") is not None:
        max_runs = _require_int(raw["max_runs"], '%s: "max_runs"' % where, minimum=1)
    if interval is None and max_runs > 1:
        raise UsageError('%s: "max_runs" > 1 requires a "repeat" pattern' % where)

    return Job(
        id=job_id,
        duration=timedelta(minutes=duration_min),
        duration_min=duration_min,
        priority=priority,
        deps=tuple(deps),
        exclusive=exclusive,
        earliest=earliest,
        latest=latest,
        repeat=repeat_text,
        interval=interval,
        max_runs=max_runs,
    )


def parse_jobs(raw: Any) -> Tuple[Job, ...]:
    if not isinstance(raw, (list, tuple)):
        raise UsageError('"jobs" must be an array, got %s' % _typename(raw))
    jobs: List[Job] = []
    seen = {}
    for index, item in enumerate(raw):
        job = _parse_job(item, index)
        if job.id in seen:
            raise UsageError('jobs[%d]: duplicate job id %r (first seen at jobs[%d])'
                             % (index, job.id, seen[job.id]))
        seen[job.id] = index
        jobs.append(job)
    return tuple(jobs)


def find_cycle(jobs: Sequence[Job]) -> Optional[List[str]]:
    """Return one dependency cycle as a list of ids, or ``None`` when acyclic."""
    by_id = {job.id: job for job in jobs}
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {job.id: WHITE for job in jobs}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        colour[node] = GREY
        stack.append(node)
        for dep in by_id[node].deps:
            if dep not in by_id:
                continue  # unknown ids are reported separately
            if colour[dep] == GREY:
                start = stack.index(dep)
                return stack[start:] + [dep]
            if colour[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        colour[node] = BLACK
        return None

    for job in jobs:
        if colour[job.id] == WHITE:
            found = visit(job.id)
            if found:
                return found
    return None


def parse_plan(raw: Any) -> Plan:
    """Validate a whole input document and return an immutable :class:`Plan`."""
    data = _as_mapping(raw, "input")
    if "window" not in data:
        raise UsageError('input is missing required key "window"')
    if "jobs" not in data:
        raise UsageError('input is missing required key "jobs"')

    window = parse_window(data["window"])
    jobs = parse_jobs(data["jobs"])

    known = {job.id for job in jobs}
    for job in jobs:  # unknown dependency ids, in input order for determinism
        for dep in job.deps:
            if dep not in known:
                raise UsageError('job %r: unknown dependency id %r' % (job.id, dep))

    cycle = find_cycle(jobs)
    if cycle:
        raise UsageError("dependency cycle detected: %s" % " -> ".join(cycle))

    return Plan(window=window, jobs=jobs)
