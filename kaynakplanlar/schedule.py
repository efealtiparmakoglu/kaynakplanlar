"""Greedy single-machine scheduler. Pure: no I/O, no printing.

Ordering rules
--------------
1. A job is only considered once *every* dependency has been placed, so the
   processing order is a topological order of the dependency graph.
2. Among the jobs that are ready, the next one is picked by ``(priority asc,
   id asc)`` -- lower priority value runs earlier, ties break on the id.

Placement rules
---------------
* ``earliest``/``latest`` constrain the **start** only, inclusively, and are
  re-evaluated every day of the window.
* A placement must end at or before ``window.end``.
* Two placements never overlap. When the earliest legal start collides, the
  job is advanced to the end of the latest colliding placement and the search
  restarts. This is the "push".
* A dependency forces ``start >= end`` of the dependency's latest placed run.
  The *wanted* start of a run is its natural candidate -- the window start for
  the first run, the recurrence anchor afterwards, rolled to ``earliest``.
  When a dependency moves the actual start past that wanted start, the push
  reports ``reason=dep``; exclusivity collisions report ``reason=exclusive``
  and plain machine collisions ``reason=collision``.
* A dependency with no placement blocks the job (and, transitively, its own
  dependents).

Recurrence is anchored on the *previous placed start*, not on its end, so the
drift is exactly ``duration - interval`` per run when the interval is shorter
than the duration.
"""

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .model import Job, Plan, Window

__all__ = ["Placement", "Warning", "ScheduleResult", "schedule_plan", "processing_order"]

PUSHED = "PUSHED"
DROPPED = "DROPPED"
BLOCKED = "BLOCKED"

REASON_COLLISION = "collision"
REASON_EXCLUSIVE = "exclusive"
REASON_DEP = "dep"
REASON_NO_SLOT = "no-slot"


@dataclass(frozen=True)
class Placement:
    """One scheduled run of one job."""

    id: str
    run: int
    start: datetime
    end: datetime
    priority: int
    exclusive: bool
    deps: Tuple[str, ...]
    pushed: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "run": self.run,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "priority": self.priority,
            "exclusive": self.exclusive,
            "deps": list(self.deps),
            "pushed": self.pushed,
        }


@dataclass(frozen=True)
class Warning:
    """A non-fatal schedule event: PUSHED, DROPPED or BLOCKED."""

    code: str
    id: str
    run: int
    detail: str

    def as_dict(self) -> Dict[str, object]:
        return {"code": self.code, "id": self.id, "run": self.run, "detail": self.detail}

    def line(self) -> str:
        # BLOCKED lines omit the run: a blocked job never got one.
        if self.code == BLOCKED:
            return "%s id=%s %s" % (self.code, self.id, self.detail)
        return "%s id=%s run=%d %s" % (self.code, self.id, self.run, self.detail)


@dataclass
class ScheduleResult:
    schedule: List[Placement] = field(default_factory=list)
    warnings: List[Warning] = field(default_factory=list)
    jobs: int = 0

    @property
    def dropped(self) -> int:
        return sum(1 for w in self.warnings if w.code == DROPPED)

    @property
    def blocked(self) -> int:
        return sum(1 for w in self.warnings if w.code == BLOCKED)

    @property
    def pushed(self) -> int:
        return sum(1 for w in self.warnings if w.code == PUSHED)

    def summary(self) -> Dict[str, int]:
        return {
            "jobs": self.jobs,
            "runs": len(self.schedule),
            "dropped": self.dropped,
            "blocked": self.blocked,
            "pushed": self.pushed,
        }


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------


def processing_order(jobs: Sequence[Job]) -> List[str]:
    """Topological order, tie-broken by ``(priority asc, id asc)``."""
    by_id = {job.id: job for job in jobs}
    indegree = {job.id: len(job.deps) for job in jobs}
    dependents: Dict[str, List[str]] = {job.id: [] for job in jobs}
    for job in jobs:
        for dep in job.deps:
            dependents[dep].append(job.id)

    ready = [(by_id[jid].priority, jid) for jid, n in indegree.items() if n == 0]
    heapq.heapify(ready)

    order: List[str] = []
    while ready:
        _, job_id = heapq.heappop(ready)
        order.append(job_id)
        for dependent in sorted(dependents[job_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, (by_id[dependent].priority, dependent))
    if len(order) != len(jobs):  # unreachable: parse_plan rejects cycles
        raise AssertionError("cycle reached the scheduler")
    return order


# --------------------------------------------------------------------------
# slot search
# --------------------------------------------------------------------------


def _minutes_of_day(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def _daily_floor(moment: datetime, job: Job) -> datetime:
    """Roll ``moment`` forward until its time of day is inside ``[earliest, latest]``."""
    if not job.has_daily_window:
        return moment
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = _minutes_of_day(moment)
    if minutes < job.earliest:
        return midnight + timedelta(minutes=job.earliest)
    if minutes > job.latest:
        return midnight + timedelta(days=1, minutes=job.earliest)
    return moment


def _conflict_end(start: datetime, job: Job, placed: Sequence[Placement]) -> Optional[datetime]:
    """End of the latest placement overlapping ``[start, start+duration)``."""
    finish = start + job.duration
    latest: Optional[datetime] = None
    for other in placed:
        if other.start < finish and start < other.end:
            if latest is None or other.end > latest:
                latest = other.end
    return latest


def _earliest_start(
    lower: datetime,
    job: Job,
    window: Window,
    placed: Sequence[Placement],
) -> Optional[datetime]:
    """First instant at or after ``lower`` where ``job`` fits, or ``None``.

    ``lower`` is expected to already be inside the window.
    """
    moment = max(lower, window.start)
    # Every iteration advances the moment strictly, so the number of possible
    # iterations is bounded by the window length in minutes.
    budget = int(window.minutes) + 2
    for _ in range(budget):
        moment = _daily_floor(moment, job)
        if moment + job.duration > window.end:
            return None
        clash = _conflict_end(moment, job, placed)
        if clash is None:
            return moment
        moment = clash
    return None


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------


def schedule_plan(plan: Plan) -> ScheduleResult:
    """Greedily place every job of ``plan`` on the single machine."""
    by_id = {job.id: job for job in plan.jobs}
    window = plan.window
    result = ScheduleResult(jobs=len(plan.jobs))
    placed: List[Placement] = []
    ends_by_id: Dict[str, List[datetime]] = {}

    for job_id in processing_order(plan.jobs):
        job = by_id[job_id]

        dep_end: Optional[datetime] = None
        missing: Optional[str] = None
        for dep in job.deps:
            dep_ends = ends_by_id.get(dep)
            if not dep_ends:
                missing = dep
                break
            newest = max(dep_ends)
            if dep_end is None or newest > dep_end:
                dep_end = newest
        if missing is not None:
            result.warnings.append(
                Warning(BLOCKED, job.id, 1, "reason=dep-missing:%s" % missing)
            )
            continue

        lower = window.start if dep_end is None else max(window.start, dep_end)
        anchor: Optional[datetime] = None
        runs = 0
        while runs < job.max_runs:
            # The natural (wanted) start ignores the dependency bound, so a
            # wait for a dependency is visible as a push with reason=dep.
            candidate = window.start if anchor is None else anchor
            wanted = _daily_floor(candidate, job)
            bound = candidate if candidate > lower else lower
            start = _earliest_start(bound, job, window, placed)
            if start is None:
                result.warnings.append(
                    Warning(DROPPED, job.id, runs + 1, "reason=%s" % REASON_NO_SLOT)
                )
                break

            pushed = start != wanted
            if pushed:
                if dep_end is not None and dep_end > wanted:
                    reason = REASON_DEP
                elif job.exclusive:
                    reason = REASON_EXCLUSIVE
                else:
                    reason = REASON_COLLISION
                result.warnings.append(
                    Warning(
                        PUSHED,
                        job.id,
                        runs + 1,
                        "from=%s to=%s reason=%s"
                        % (wanted.isoformat(), start.isoformat(), reason),
                    )
                )

            placement = Placement(
                id=job.id,
                run=runs + 1,
                start=start,
                end=start + job.duration,
                priority=job.priority,
                exclusive=job.exclusive,
                deps=job.deps,
                pushed=pushed,
            )
            placed.append(placement)
            ends_by_id.setdefault(job.id, []).append(placement.end)
            runs += 1

            if job.interval is not None and runs < job.max_runs:
                anchor = start + job.interval  # drift is intentional; see module docstring

    result.schedule = sorted(placed, key=lambda p: (p.start, p.id, p.run))
    return result
