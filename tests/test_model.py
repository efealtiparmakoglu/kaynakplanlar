"""Tests for input parsing and validation (kaynakplanlar.model)."""

import unittest
from datetime import datetime, timedelta, timezone

from kaynakplanlar.model import (
    Job,
    Plan,
    UsageError,
    Window,
    parse_clock,
    parse_iso,
    parse_jobs,
    parse_plan,
    parse_window,
)

UTC = timezone.utc


def doc(**overrides):
    """A minimal valid input document with per-key overrides for jobs."""
    jobs = overrides.pop("jobs", [{"id": "a", "duration_min": 10}])
    window = overrides.pop(
        "window",
        {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-02T00:00:00+00:00"},
    )
    return {"window": window, "jobs": jobs}


class WindowParsingTests(unittest.TestCase):
    def test_valid_window(self):
        w = parse_window({"start": "2026-10-01T00:00:00+00:00",
                          "end": "2026-10-02T00:00:00+00:00"})
        self.assertEqual(w.start, datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(w.end, datetime(2026, 10, 2, tzinfo=UTC))
        self.assertEqual(w.minutes, 24 * 60)

    def test_trailing_z_is_accepted(self):
        w = parse_window({"start": "2026-10-01T00:00:00Z", "end": "2026-10-01T01:00:00Z"})
        self.assertEqual(w.minutes, 60)

    def test_naive_datetimes_become_utc(self):
        w = parse_window({"start": "2026-10-01T00:00:00", "end": "2026-10-01T02:00:00"})
        self.assertEqual(w.start.tzinfo, UTC)

    def test_non_utc_offset_is_normalised(self):
        w = parse_window({"start": "2026-10-01T02:00:00+02:00", "end": "2026-10-01T06:00:00+02:00"})
        self.assertEqual(w.start, datetime(2026, 10, 1, 0, 0, tzinfo=UTC))

    def test_end_not_after_start_is_a_usage_error(self):
        with self.assertRaises(UsageError) as ctx:
            parse_window({"start": "2026-10-02T00:00:00+00:00", "end": "2026-10-01T00:00:00+00:00"})
        self.assertIn("window.end", str(ctx.exception))

    def test_missing_keys(self):
        with self.assertRaises(UsageError):
            parse_window({"start": "2026-10-01T00:00:00+00:00"})
        with self.assertRaises(UsageError):
            parse_window({})

    def test_bad_iso(self):
        with self.assertRaises(UsageError):
            parse_window({"start": "not-a-date", "end": "2026-10-01T01:00:00+00:00"})


class ClockParsingTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_clock("02:00", "t"), 120)
        self.assertEqual(parse_clock("9:05", "t"), 9 * 60 + 5)
        self.assertEqual(parse_clock("23:59", "t"), 24 * 60 - 1)

    def test_invalid(self):
        for bad in ("24:00", "12:60", "0200", "2h", "", "12:00pm"):
            with self.assertRaises(UsageError, msg=bad):
                parse_clock(bad, "t")
        with self.assertRaises(UsageError):
            parse_clock(90, "t")


class IsoParsingTests(unittest.TestCase):
    def test_rejects_non_string(self):
        with self.assertRaises(UsageError):
            parse_iso(20261001, "x")


class JobParsingTests(unittest.TestCase):
    def test_defaults(self):
        job = parse_jobs([{"id": "a", "duration_min": 10}])[0]
        self.assertEqual(job.priority, 100)
        self.assertEqual(job.deps, ())
        self.assertFalse(job.exclusive)
        self.assertEqual(job.max_runs, 1)
        self.assertIsNone(job.interval)
        self.assertEqual(job.repeat, "")
        self.assertFalse(job.has_daily_window)

    def test_id_is_required(self):
        with self.assertRaises(UsageError):
            parse_jobs([{"duration_min": 10}])
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "  ", "duration_min": 10}])

    def test_duration_min_is_required_positive_int(self):
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a"}])
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a", "duration_min": 0}])
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a", "duration_min": -5}])
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a", "duration_min": 1.5}])
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a", "duration_min": True}])
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a", "duration_min": "30"}])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(UsageError) as ctx:
            parse_jobs([{"id": "a", "duration_min": 5}, {"id": "a", "duration_min": 5}])
        self.assertIn("duplicate", str(ctx.exception))

    def test_exclusive_must_be_boolean(self):
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a", "duration_min": 5, "exclusive": "yes"}])

    def test_earliest_latest_bounds(self):
        job = parse_jobs([{"id": "a", "duration_min": 5, "earliest": "02:00", "latest": "05:00"}])[0]
        self.assertEqual(job.earliest, 120)
        self.assertEqual(job.latest, 300)
        self.assertTrue(job.has_daily_window)

    def test_earliest_after_latest_rejected(self):
        with self.assertRaises(UsageError) as ctx:
            parse_jobs([{"id": "a", "duration_min": 5, "earliest": "05:00", "latest": "02:00"}])
        self.assertIn("earliest", str(ctx.exception))

    def test_bad_earliest_names_the_job(self):
        with self.assertRaises(UsageError) as ctx:
            parse_jobs([{"id": "a", "duration_min": 5, "earliest": "25:00"}])
        self.assertIn("'a'", str(ctx.exception))


class RepeatParsingTests(unittest.TestCase):
    def test_every_nmin(self):
        job = parse_jobs([{"id": "a", "duration_min": 5, "repeat": "every 30min"}])[0]
        self.assertEqual(job.interval, timedelta(minutes=30))
        self.assertEqual(job.repeat, "every 30min")

    def test_every_nh(self):
        job = parse_jobs([{"id": "a", "duration_min": 5, "repeat": "every 2h"}])[0]
        self.assertEqual(job.interval, timedelta(hours=2))
        self.assertEqual(job.repeat, "every 2h")

    def test_every_nd(self):
        job = parse_jobs([{"id": "a", "duration_min": 5, "repeat": "every 2d"}])[0]
        self.assertEqual(job.interval, timedelta(days=2))
        self.assertEqual(job.repeat, "every 2d")

    def test_daily(self):
        job = parse_jobs([{"id": "a", "duration_min": 5, "repeat": "daily"}])[0]
        self.assertEqual(job.interval, timedelta(days=1))
        self.assertEqual(job.repeat, "daily")

    def test_bad_pattern(self):
        for bad in ("hourly", "every", "every -5min", "every 0min", "weekly"):
            with self.assertRaises(UsageError, msg=bad):
                parse_jobs([{"id": "a", "duration_min": 5, "repeat": bad}])

    def test_max_runs_without_repeat_rejected(self):
        with self.assertRaises(UsageError):
            parse_jobs([{"id": "a", "duration_min": 5, "max_runs": 3}])

    def test_max_runs_must_be_positive_int(self):
        for bad in (0, -1, 1.5, "3"):
            with self.assertRaises(UsageError, msg=str(bad)):
                parse_jobs([{"id": "a", "duration_min": 5, "repeat": "daily", "max_runs": bad}])


class DependencyValidationTests(unittest.TestCase):
    def test_unknown_dependency_names_job_and_dep(self):
        with self.assertRaises(UsageError) as ctx:
            parse_plan(doc(jobs=[{"id": "a", "duration_min": 5, "deps": ["zz"]}]))
        self.assertIn("'a'", str(ctx.exception))
        self.assertIn("'zz'", str(ctx.exception))

    def test_self_dependency_is_a_cycle(self):
        with self.assertRaises(UsageError) as ctx:
            parse_plan(doc(jobs=[{"id": "a", "duration_min": 5, "deps": ["a"]}]))
        self.assertIn("cycle", str(ctx.exception))

    def test_two_node_cycle(self):
        with self.assertRaises(UsageError) as ctx:
            parse_plan(doc(jobs=[{"id": "a", "duration_min": 5, "deps": ["b"]},
                                 {"id": "b", "duration_min": 5, "deps": ["a"]}]))
        self.assertIn("a -> b -> a", str(ctx.exception))

    def test_three_node_cycle(self):
        with self.assertRaises(UsageError) as ctx:
            parse_plan(doc(jobs=[{"id": "a", "duration_min": 5, "deps": ["c"]},
                                 {"id": "b", "duration_min": 5, "deps": ["a"]},
                                 {"id": "c", "duration_min": 5, "deps": ["b"]}]))
        self.assertIn("cycle", str(ctx.exception))

    def test_diamond_is_not_a_cycle(self):
        plan = parse_plan(doc(jobs=[
            {"id": "root", "duration_min": 5},
            {"id": "left", "duration_min": 5, "deps": ["root"]},
            {"id": "right", "duration_min": 5, "deps": ["root"]},
            {"id": "sink", "duration_min": 5, "deps": ["left", "right"]},
        ]))
        self.assertEqual(len(plan.jobs), 4)


class PlanLevelTests(unittest.TestCase):
    def test_missing_top_level_keys(self):
        with self.assertRaises(UsageError):
            parse_plan({"window": {"start": "2026-10-01T00:00:00+00:00",
                                   "end": "2026-10-01T01:00:00+00:00"}})
        with self.assertRaises(UsageError):
            parse_plan({"jobs": []})

    def test_jobs_must_be_array(self):
        with self.assertRaises(UsageError):
            parse_plan(doc(jobs={"id": "a"}))

    def test_job_and_ids_accessors(self):
        plan = parse_plan(doc(jobs=[{"id": "b", "duration_min": 5},
                                    {"id": "a", "duration_min": 5}]))
        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.ids, ("b", "a"))
        self.assertEqual(plan.job("a").id, "a")
        with self.assertRaises(KeyError):
            plan.job("nope")
        self.assertIsInstance(plan.window, Window)
        self.assertIsInstance(plan.jobs[0], Job)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
