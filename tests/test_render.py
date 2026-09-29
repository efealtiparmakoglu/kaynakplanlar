"""Tests for output rendering (kaynakplanlar.render)."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from kaynakplanlar.model import parse_plan
from kaynakplanlar.render import (
    DEFAULT_BAR_WIDTH,
    bar_for,
    build_json,
    dumps_json,
    load_rows,
    render_load,
    render_summary,
    render_table,
    render_warnings,
)
from kaynakplanlar.schedule import Placement, ScheduleResult, Warning, schedule_plan

UTC = timezone.utc
PUSHED_CODE = "PUSHED"


def placement(pid="a", run=1, start_h=0, minutes=60, priority=1,
              exclusive=False, deps=(), pushed=False):
    start = datetime(2026, 10, 1, start_h, 0, tzinfo=UTC)
    return Placement(
        id=pid, run=run, start=start, end=start + timedelta(minutes=minutes),
        priority=priority, exclusive=exclusive, deps=tuple(deps), pushed=pushed,
    )


class TableTests(unittest.TestCase):
    def test_header_and_row_columns(self):
        table = render_table([placement(pid="build", deps=("fetch",))])
        lines = table.splitlines()
        self.assertEqual(lines[0].split(), ["start_iso", "end_iso", "id", "run#", "priority", "deps"])
        row = lines[2].split()
        self.assertEqual(row[2], "build")
        self.assertEqual(row[3], "1")
        self.assertEqual(row[4], "1")
        self.assertEqual(row[5], "fetch")
        self.assertIn("2026-10-01T00:00:00+00:00", lines[2])
        self.assertIn("2026-10-01T01:00:00+00:00", lines[2])

    def test_dash_placeholder_for_no_deps(self):
        row = render_table([placement()]).splitlines()[2]
        self.assertTrue(row.rstrip().endswith("-"))

    def test_empty_table(self):
        table = render_table([])
        self.assertIn("start_iso", table)

    def test_iso_is_utc(self):
        self.assertIn("+00:00", render_table([placement()]))


class WarningAndSummaryTests(unittest.TestCase):
    def test_warning_lines(self):
        warning = Warning(PUSHED_CODE, "a", 2,
                          "from=2026-10-01T00:00:00+00:00 to=2026-10-01T01:00:00+00:00 reason=collision")
        self.assertEqual(
            render_warnings([warning]),
            ["PUSHED id=a run=2 from=2026-10-01T00:00:00+00:00 "
             "to=2026-10-01T01:00:00+00:00 reason=collision"],
        )
        self.assertEqual(warning.as_dict(),
                         {"code": "PUSHED", "id": "a", "run": 2,
                          "detail": warning.detail})

    def test_summary_line(self):
        self.assertEqual(render_summary({"jobs": 4, "runs": 5, "dropped": 1,
                                         "blocked": 0, "pushed": 2}),
                         "jobs=4 runs=5 dropped=1 blocked=0 pushed=2")


class LoadTests(unittest.TestCase):
    WINDOW = parse_plan({
        "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T03:00:00+00:00"},
        "jobs": [{"id": "a", "duration_min": 60}],
    }).window

    def test_buckets_split_at_hour_boundaries(self):
        rows = load_rows(self.WINDOW, [placement(start_h=0, minutes=90)])
        by_hour = {h: m for h, m in rows}
        self.assertEqual(by_hour[datetime(2026, 10, 1, 0, tzinfo=UTC)], 60.0)
        self.assertEqual(by_hour[datetime(2026, 10, 1, 1, tzinfo=UTC)], 30.0)
        self.assertEqual(by_hour[datetime(2026, 10, 1, 2, tzinfo=UTC)], 0.0)

    def test_only_bins_inside_the_window(self):
        rows = load_rows(self.WINDOW, [])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1][0], datetime(2026, 10, 1, 2, tzinfo=UTC))

    def test_trailing_bin_trimmed_for_hour_aligned_end(self):
        rows = load_rows(self.WINDOW, [])
        last = rows[-1][0] + timedelta(hours=1)
        self.assertEqual(last, self.WINDOW.end)

    def test_bar_scales_to_busiest_hour(self):
        self.assertEqual(len(bar_for(60.0, 60.0, 20)), 20)
        self.assertEqual(len(bar_for(30.0, 60.0, 20)), 10)
        self.assertEqual(len(bar_for(15.0, 60.0, 20)), 5)

    def test_bar_minimum_one_cell_for_nonzero_minutes(self):
        self.assertEqual(bar_for(1.0, 60.0, 20), "█")

    def test_bar_empty_for_zero(self):
        self.assertEqual(bar_for(0.0, 60.0, 20), "")
        self.assertEqual(bar_for(10.0, 0.0, 20), "")

    def test_bar_respects_width_argument(self):
        self.assertEqual(len(bar_for(60.0, 60.0, 7)), 7)

    def test_render_load_reports_busiest(self):
        text = render_load(self.WINDOW, [placement(minutes=30)], width=10)
        self.assertIn("busiest hour: 30 min", text)
        self.assertIn("█", text)

    def test_render_load_of_empty_schedule(self):
        text = render_load(self.WINDOW, [])
        self.assertIn("busiest hour: 0 min", text)


class JsonTests(unittest.TestCase):
    def build(self, with_load=False, bar_width=None):
        plan = parse_plan({
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T03:00:00+00:00"},
            "jobs": [
                {"id": "a", "duration_min": 30, "priority": 1, "deps": ["b"], "exclusive": True},
                {"id": "b", "duration_min": 30, "priority": 0},
                {"id": "c", "duration_min": 200, "priority": 9},
            ],
        })
        result = schedule_plan(plan)
        self.assertTrue(result.warnings)  # c is dropped
        if with_load:
            return plan, result, build_json(result, window=plan.window,
                                            placements=result.schedule,
                                            bar_width=bar_width)
        return plan, result, build_json(result)

    def test_top_level_keys(self):
        _, _, payload = self.build()
        self.assertEqual(set(payload), {"schedule", "warnings", "summary"})

    def test_schedule_items(self):
        _, _, payload = self.build()
        first = payload["schedule"][0]
        self.assertEqual(set(first),
                         {"id", "run", "start", "end", "priority", "exclusive",
                          "deps", "pushed"})
        self.assertEqual(first["id"], "b")
        self.assertEqual(first["deps"], [])
        self.assertFalse(first["pushed"])
        child = payload["schedule"][1]
        self.assertEqual(child["deps"], ["b"])
        self.assertTrue(child["pushed"])
        self.assertTrue(child["exclusive"])

    def test_schedule_sorted_by_start(self):
        _, _, payload = self.build()
        starts = [item["start"] for item in payload["schedule"]]
        self.assertEqual(starts, sorted(starts))

    def test_warnings_items(self):
        _, _, payload = self.build()
        dropped = [w for w in payload["warnings"] if w["code"] == "DROPPED"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["id"], "c")
        self.assertEqual(dropped[0]["run"], 1)
        self.assertIn("reason=no-slot", dropped[0]["detail"])

    def test_summary_block(self):
        _, _, payload = self.build()
        self.assertEqual(payload["summary"],
                         {"jobs": 3, "runs": 2, "dropped": 1, "blocked": 0, "pushed": 1})

    def test_load_block_only_when_requested(self):
        _, _, payload = self.build(with_load=True, bar_width=10)
        self.assertIn("load", payload)
        self.assertEqual(len(payload["load"]), 3)
        entry = payload["load"][0]
        self.assertEqual(set(entry), {"hour", "minutes", "bar"})
        self.assertEqual(len(entry["bar"]), 10)
        _, _, bare = self.build()
        self.assertNotIn("load", bare)

    def test_dumps_json_is_parseable_and_newline_terminated(self):
        _, _, payload = self.build()
        text = dumps_json(payload)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), payload)

    def test_default_bar_width(self):
        self.assertEqual(DEFAULT_BAR_WIDTH, 20)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
