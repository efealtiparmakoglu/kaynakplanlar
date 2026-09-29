"""Tests for the greedy scheduler (kaynakplanlar.schedule)."""

import unittest
from datetime import datetime, timezone

from kaynakplanlar.model import parse_plan
from kaynakplanlar.schedule import (
    BLOCKED,
    DROPPED,
    PUSHED,
    processing_order,
    schedule_plan,
)

UTC = timezone.utc
DAY = "2026-10-01"


def at(hour, minute=0):
    return datetime(2026, 10, 1, hour, minute, tzinfo=UTC)


def plan(window_end="2026-10-02T00:00:00+00:00", window_start="2026-10-01T00:00:00+00:00", jobs=()):
    return parse_plan({
        "window": {"start": window_start, "end": window_end},
        "jobs": list(jobs),
    })


def by_id(result):
    return {p.id + "#%d" % p.run: p for p in result.schedule}


class ProcessingOrderTests(unittest.TestCase):
    def test_dependencies_come_first(self):
        jobs = plan(jobs=[
            {"id": "c", "duration_min": 5, "deps": ["b"]},
            {"id": "b", "duration_min": 5, "deps": ["a"]},
            {"id": "a", "duration_min": 5},
        ]).jobs
        self.assertEqual(processing_order(jobs), ["a", "b", "c"])

    def test_priority_then_id_among_ready_jobs(self):
        jobs = plan(jobs=[
            {"id": "z", "duration_min": 5, "priority": 1},
            {"id": "a", "duration_min": 5, "priority": 0},
            {"id": "m", "duration_min": 5, "priority": 0},
        ]).jobs
        self.assertEqual(processing_order(jobs), ["a", "m", "z"])

    def test_priority_beats_alphabet_for_ready_set(self):
        jobs = plan(jobs=[
            {"id": "zzz", "duration_min": 5, "priority": 0},
            {"id": "aaa", "duration_min": 5, "priority": 5},
        ]).jobs
        self.assertEqual(processing_order(jobs), ["zzz", "aaa"])


class BasicPlacementTests(unittest.TestCase):
    def test_single_job_starts_at_window_start(self):
        result = schedule_plan(plan(jobs=[{"id": "a", "duration_min": 30}]))
        self.assertEqual(len(result.schedule), 1)
        p = result.schedule[0]
        self.assertEqual(p.start, at(0))
        self.assertEqual(p.end, at(0, 30))
        self.assertFalse(p.pushed)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.summary(), {"jobs": 1, "runs": 1, "dropped": 0,
                                            "blocked": 0, "pushed": 0})

    def test_one_machine_never_overlaps(self):
        result = schedule_plan(plan(jobs=[
            {"id": "a", "duration_min": 60},
            {"id": "b", "duration_min": 60},
        ]))
        starts = sorted((p.start, p.end) for p in result.schedule)
        self.assertEqual(starts, [(at(0), at(1)), (at(1), at(2))])

    def test_priority_zero_runs_before_higher_values(self):
        result = schedule_plan(plan(jobs=[
            {"id": "later", "duration_min": 10, "priority": 1},
            {"id": "first", "duration_min": 10, "priority": 0},
        ]))
        self.assertEqual(result.schedule[0].id, "first")

    def test_id_breaks_priority_ties(self):
        result = schedule_plan(plan(jobs=[
            {"id": "beta", "duration_min": 10, "priority": 1},
            {"id": "alpha", "duration_min": 10, "priority": 1},
        ]))
        self.assertEqual([p.id for p in result.schedule], ["alpha", "beta"])

    def test_schedule_is_sorted_by_start(self):
        result = schedule_plan(plan(jobs=[
            {"id": "slow", "duration_min": 120, "priority": 0},
            {"id": "quick", "duration_min": 10, "priority": 9},
        ]))
        starts = [p.start for p in result.schedule]
        self.assertEqual(starts, sorted(starts))

    def test_exact_fit_to_window_end_is_allowed(self):
        result = schedule_plan(plan(
            window_end="2026-10-01T01:00:00+00:00",
            jobs=[{"id": "a", "duration_min": 60}],
        ))
        self.assertEqual(len(result.schedule), 1)
        self.assertEqual(result.schedule[0].end, at(1))

    def test_duration_longer_than_window_is_dropped(self):
        result = schedule_plan(plan(
            window_end="2026-10-01T01:00:00+00:00",
            jobs=[{"id": "a", "duration_min": 61}],
        ))
        self.assertEqual(result.schedule, [])
        self.assertEqual(result.warnings[0].code, DROPPED)
        self.assertEqual(result.warnings[0].run, 1)
        self.assertIn("reason=no-slot", result.warnings[0].detail)
        self.assertEqual(result.dropped, 1)


class DailyWindowTests(unittest.TestCase):
    def test_start_rolled_to_earliest(self):
        result = schedule_plan(plan(jobs=[
            {"id": "a", "duration_min": 10, "earliest": "02:00"},
        ]))
        self.assertEqual(result.schedule[0].start, at(2))

    def test_earliest_and_latest_are_inclusive(self):
        result = schedule_plan(plan(jobs=[
            {"id": "edge", "duration_min": 10, "priority": 0,
             "earliest": "05:00", "latest": "06:00"},
        ]))
        self.assertEqual(result.schedule[0].start, at(5))

    def test_start_after_latest_rolls_to_next_day_and_drops(self):
        # Only reachable through a push; a lone job is simply placed at earliest.
        result = schedule_plan(plan(
            window_end="2026-10-01T07:00:00+00:00",
            jobs=[
                {"id": "blocker", "duration_min": 360, "priority": 0},
                {"id": "late", "duration_min": 60, "priority": 1,
                 "earliest": "01:00", "latest": "02:00"},
            ],
        ))
        placed = by_id(result)
        # wanted 01:00 collides with blocker (00:00-06:00); 06:00 is past
        # latest=02:00, so it rolls to next day 01:00 -- beyond the window.
        self.assertNotIn("late#1", placed)
        self.assertEqual(result.dropped, 1)
        self.assertIn("reason=no-slot", result.warnings[0].detail)

    def test_fit_only_constrains_the_start(self):
        # The job may END after `latest` as long as the START is within bounds.
        result = schedule_plan(plan(
            window_end="2026-10-01T08:00:00+00:00",
            jobs=[{"id": "a", "duration_min": 120, "earliest": "06:00", "latest": "07:00"}],
        ))
        self.assertEqual(result.schedule[0].start, at(6))
        self.assertEqual(result.schedule[0].end, at(8))


class PushTests(unittest.TestCase):
    def test_collision_push_carries_from_to_and_reason(self):
        result = schedule_plan(plan(jobs=[
            {"id": "big", "duration_min": 60, "priority": 0},
            {"id": "small", "duration_min": 10, "priority": 1},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["small#1"].start, at(1))
        self.assertTrue(placed["small#1"].pushed)
        warning = result.warnings[0]
        self.assertEqual(warning.code, PUSHED)
        self.assertEqual(warning.id, "small")
        self.assertEqual(warning.run, 1)
        self.assertIn("from=%sT00:00:00+00:00" % DAY, warning.detail)
        self.assertIn("to=%sT01:00:00+00:00" % DAY, warning.detail)
        self.assertIn("reason=collision", warning.detail)

    def test_exclusive_job_push_reports_reason_exclusive(self):
        result = schedule_plan(plan(jobs=[
            {"id": "plain", "duration_min": 30, "priority": 0},
            {"id": "solo", "duration_min": 10, "priority": 1, "exclusive": True},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["solo#1"].start, at(0, 30))
        self.assertIn("reason=exclusive", result.warnings[0].detail)

    def test_unpushed_job_has_no_warning(self):
        result = schedule_plan(plan(jobs=[
            {"id": "a", "duration_min": 60, "priority": 0},
            {"id": "b", "duration_min": 10, "priority": 1, "earliest": "01:00"},
        ]))
        self.assertEqual([w.code for w in result.warnings], [])
        self.assertFalse(any(p.pushed for p in result.schedule))

    def test_push_advances_past_latest_overlapping_placement(self):
        result = schedule_plan(plan(jobs=[
            {"id": "a", "duration_min": 60, "priority": 0},   # 00:00-01:00
            {"id": "b", "duration_min": 120, "priority": 0},  # pushed past a? no: a first
            {"id": "c", "duration_min": 10, "priority": 9},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["c#1"].start, at(3))


class DependencyTests(unittest.TestCase):
    def test_dependent_starts_at_dependency_end(self):
        result = schedule_plan(plan(jobs=[
            {"id": "child", "duration_min": 30, "priority": 0, "deps": ["parent"]},
            {"id": "parent", "duration_min": 45, "priority": 0},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["parent#1"].end, at(0, 45))
        self.assertEqual(placed["child#1"].start, at(0, 45))

    def test_dep_push_reports_reason_dep(self):
        result = schedule_plan(plan(jobs=[
            {"id": "child", "duration_min": 10, "priority": 0, "deps": ["parent"]},
            {"id": "parent", "duration_min": 60, "priority": 0},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["child#1"].start, at(1))
        self.assertTrue(placed["child#1"].pushed)
        warning = result.warnings[0]
        self.assertEqual(warning.code, PUSHED)
        self.assertEqual(warning.id, "child")
        self.assertIn("from=%sT00:00:00+00:00" % DAY, warning.detail)
        self.assertIn("to=%sT01:00:00+00:00" % DAY, warning.detail)
        self.assertIn("reason=dep", warning.detail)

    def test_daily_window_can_absorb_a_dependency_wait(self):
        # The child could never have started before 02:00 anyway, so the
        # dependency wait causes no push.
        result = schedule_plan(plan(jobs=[
            {"id": "child", "duration_min": 10, "priority": 0,
             "deps": ["parent"], "earliest": "02:00"},
            {"id": "parent", "duration_min": 60, "priority": 0},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["child#1"].start, at(2))
        self.assertFalse(placed["child#1"].pushed)
        self.assertEqual(result.warnings, [])

    def test_multiple_deps_use_the_latest_end(self):
        result = schedule_plan(plan(jobs=[
            {"id": "sink", "duration_min": 10, "priority": 0, "deps": ["short", "long"]},
            {"id": "short", "duration_min": 10, "priority": 0},
            {"id": "long", "duration_min": 120, "priority": 1},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["sink#1"].start, at(2, 10))

    def test_dropped_dependency_blocks_the_dependent(self):
        result = schedule_plan(plan(
            window_end="2026-10-01T01:00:00+00:00",
            jobs=[
                {"id": "doomed", "duration_min": 120, "priority": 0},
                {"id": "child", "duration_min": 10, "priority": 1, "deps": ["doomed"]},
            ],
        ))
        self.assertEqual(result.schedule, [])
        codes = [(w.code, w.id, w.detail) for w in result.warnings]
        self.assertIn((DROPPED, "doomed", "reason=no-slot"), codes)
        self.assertIn((BLOCKED, "child", "reason=dep-missing:doomed"), codes)

    def test_blocking_is_transitive(self):
        result = schedule_plan(plan(
            window_end="2026-10-01T01:00:00+00:00",
            jobs=[
                {"id": "doomed", "duration_min": 120},
                {"id": "mid", "duration_min": 10, "deps": ["doomed"]},
                {"id": "leaf", "duration_min": 10, "deps": ["mid"]},
            ],
        ))
        self.assertEqual(result.schedule, [])
        blocked = {w.id for w in result.warnings if w.code == BLOCKED}
        self.assertEqual(blocked, {"mid", "leaf"})

    def test_partially_repeated_dependency_does_not_block(self):
        result = schedule_plan(plan(
            window_end="2026-10-02T00:00:00+00:00",
            jobs=[
                {"id": "dep", "duration_min": 10, "repeat": "every 8h", "max_runs": 3},
                {"id": "child", "duration_min": 10, "deps": ["dep"]},
            ],
        ))
        self.assertIn("child#1", by_id(result))
        self.assertEqual(result.blocked, 0)


class RecurrenceTests(unittest.TestCase):
    def test_interval_anchored_on_previous_placed_start(self):
        result = schedule_plan(plan(jobs=[
            {"id": "tick", "duration_min": 10, "repeat": "every 60min", "max_runs": 3},
        ]))
        starts = [p.start for p in result.schedule]
        self.assertEqual(starts, [at(0), at(1), at(2)])
        self.assertEqual([p.run for p in result.schedule], [1, 2, 3])

    def test_drift_when_interval_shorter_than_duration(self):
        result = schedule_plan(plan(jobs=[
            {"id": "tick", "duration_min": 30, "repeat": "every 10min", "max_runs": 3},
        ]))
        placed = by_id(result)
        # run 2 wants 00:10 (run 1 start + 10min) but collides -> 00:30.
        self.assertEqual(placed["tick#2"].start, at(0, 30))
        self.assertEqual(placed["tick#3"].start, at(1))
        pushed = [(w.id, w.run) for w in result.warnings if w.code == PUSHED]
        self.assertEqual(pushed, [("tick", 2), ("tick", 3)])

    def test_daily_recurrence_rolls_to_next_day(self):
        result = schedule_plan(plan(
            window_end="2026-10-03T00:00:00+00:00",
            jobs=[{"id": "nightly", "duration_min": 60, "repeat": "daily", "max_runs": 2}],
        ))
        starts = [p.start for p in result.schedule]
        self.assertEqual(starts[0], at(0))
        self.assertEqual(starts[1], datetime(2026, 10, 2, 0, 0, tzinfo=UTC))

    def test_daily_recurrence_beyond_the_window_is_dropped(self):
        result = schedule_plan(plan(jobs=[
            {"id": "nightly", "duration_min": 60, "repeat": "daily", "max_runs": 2},
        ]))
        self.assertEqual(len(result.schedule), 1)
        dropped = [w for w in result.warnings if w.code == DROPPED]
        self.assertEqual([(w.id, w.run) for w in dropped], [("nightly", 2)])
        self.assertIn("reason=no-slot", dropped[0].detail)

    def test_recurrence_honours_daily_window_and_stops_when_dropped(self):
        result = schedule_plan(plan(jobs=[
            {"id": "tests", "duration_min": 45, "repeat": "every 2h",
             "max_runs": 3, "earliest": "02:00", "latest": "05:00"},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["tests#1"].start, at(2))
        self.assertEqual(placed["tests#2"].start, at(4))
        self.assertNotIn("tests#3", placed)
        dropped = [w for w in result.warnings if w.code == DROPPED]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0].run, 3)
        self.assertIn("reason=no-slot", dropped[0].detail)

    def test_repeated_dependency_uses_latest_dep_end(self):
        result = schedule_plan(plan(jobs=[
            {"id": "fetch", "duration_min": 10, "repeat": "every 60min", "max_runs": 2},
            {"id": "build", "duration_min": 10, "deps": ["fetch"]},
        ]))
        placed = by_id(result)
        self.assertEqual(placed["fetch#2"].end, at(1, 10))
        self.assertEqual(placed["build#1"].start, at(1, 10))

    def test_recurrence_respects_exclusivity_of_other_jobs(self):
        result = schedule_plan(plan(jobs=[
            {"id": "solo", "duration_min": 60, "priority": 0, "exclusive": True},
            {"id": "tick", "duration_min": 10, "priority": 1,
             "repeat": "every 30min", "max_runs": 3},
        ]))
        placed = by_id(result)
        # solo takes 00:00-01:00; tick starts at 01:00 and repeats at 01:30, 02:00.
        self.assertEqual(placed["tick#1"].start, at(1))
        self.assertEqual(placed["tick#2"].start, at(1, 30))
        self.assertEqual(placed["tick#3"].start, at(2))
        spans = sorted((p.start, p.end) for p in result.schedule)
        for (_, prev_end), (start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(prev_end, start)


class SummaryTests(unittest.TestCase):
    def test_full_summary_counters(self):
        result = schedule_plan(plan(
            window_end="2026-10-01T02:00:00+00:00",
            jobs=[
                {"id": "fits", "duration_min": 60, "priority": 0},
                {"id": "pushed", "duration_min": 30, "priority": 1},
                {"id": "doomed", "duration_min": 300, "priority": 2},
            ],
        ))
        self.assertEqual(result.summary(),
                         {"jobs": 3, "runs": 2, "dropped": 1, "blocked": 0, "pushed": 1})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
