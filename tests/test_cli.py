"""End-to-end tests for the command line entry point (kaynakplanlar.cli)."""

import io
import json
import os
import tempfile
import unittest

from kaynakplanlar.cli import main

SPEC_EXAMPLE = {
    "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-02T00:00:00+00:00"},
    "jobs": [
        {"id": "build-core", "duration_min": 30, "priority": 1, "deps": ["fetch"]},
        {"id": "fetch", "duration_min": 10, "priority": 1},
        {"id": "migrate", "duration_min": 15, "priority": 0, "exclusive": True},
        {"id": "tests", "duration_min": 45, "priority": 2, "deps": ["build-core"],
         "earliest": "02:00", "latest": "05:00", "repeat": "every 2h", "max_runs": 3},
    ],
}


def run(argv, document=None, stdin_text=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    stdin = io.StringIO(stdin_text if stdin_text is not None
                        else (json.dumps(document) if document is not None else ""))
    code = main(argv, stdout=stdout, stderr=stderr, stdin=stdin)
    return code, stdout.getvalue(), stderr.getvalue()


class TempFileMixin(unittest.TestCase):
    def write_plan(self, document):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(document, handle)
        finally:
            handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name


class HappyPathTests(TempFileMixin):
    def test_table_summary_and_stderr_warnings(self):
        path = self.write_plan(SPEC_EXAMPLE)
        code, out, err = run([path])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(),
                         ["start_iso", "end_iso", "id", "run#", "priority", "deps"])
        rows = [line for line in lines if "2026-10-" in line and "->" not in line]
        ids = [row.split()[2] for row in rows]
        self.assertEqual(ids, ["migrate", "fetch", "build-core", "tests", "tests"])
        self.assertIn("jobs=4 runs=5 dropped=1 blocked=0 pushed=2", out)
        self.assertIn("DROPPED id=tests run=3 reason=no-slot", err)
        self.assertIn("PUSHED id=fetch run=1", err)
        self.assertIn("reason=collision", err)
        self.assertIn("PUSHED id=build-core run=1", err)
        self.assertIn("reason=dep", err)

    def test_stdin_is_default_input(self):
        code, out, _ = run([], document=SPEC_EXAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("migrate", out)

    def test_explicit_dash_reads_stdin(self):
        code, out, _ = run(["-"], document=SPEC_EXAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("migrate", out)

    def test_warnings_do_not_fail_the_run(self):
        path = self.write_plan(SPEC_EXAMPLE)
        code, _, _ = run([path])
        self.assertEqual(code, 0)

    def test_clean_run_has_empty_stderr(self):
        clean = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T01:00:00+00:00"},
            "jobs": [{"id": "a", "duration_min": 30}],
        }
        code, out, err = run([self.write_plan(clean)])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


class JsonOutputTests(TempFileMixin):
    def test_json_document_shape(self):
        path = self.write_plan(SPEC_EXAMPLE)
        code, out, _ = run([path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"schedule", "warnings", "summary"})
        self.assertEqual(payload["summary"],
                         {"jobs": 4, "runs": 5, "dropped": 1, "blocked": 0, "pushed": 2})
        first = payload["schedule"][0]
        self.assertEqual(set(first), {"id", "run", "start", "end", "priority",
                                      "exclusive", "deps", "pushed"})
        self.assertEqual(first["id"], "migrate")
        self.assertTrue(first["exclusive"])
        starts = [item["start"] for item in payload["schedule"]]
        self.assertEqual(starts, sorted(starts))
        codes = sorted(w["code"] for w in payload["warnings"])
        self.assertEqual(codes, ["DROPPED", "PUSHED", "PUSHED"])
        reasons = sorted(w["detail"].rsplit("=", 1)[1] for w in payload["warnings"]
                         if w["code"] == "PUSHED")
        self.assertEqual(reasons, ["collision", "dep"])

    def test_json_with_load_block(self):
        path = self.write_plan(SPEC_EXAMPLE)
        code, out, _ = run([path, "--json", "--load"])
        payload = json.loads(out)
        self.assertEqual(len(payload["load"]), 24)  # one bin per hour of the window
        busiest = payload["load"][0]
        self.assertEqual(busiest["minutes"], 55)
        self.assertEqual(len(busiest["bar"]), 20)  # default --bar
        self.assertTrue(set(entry["bar"]) <= {"█"} for entry in payload["load"])

    def test_bar_flag_scales_the_json_bars(self):
        path = self.write_plan(SPEC_EXAMPLE)
        _, out, _ = run([path, "--json", "--load", "--bar", "5"])
        busiest = json.loads(out)["load"][0]
        self.assertEqual(len(busiest["bar"]), 5)

    def test_bar_flag_scales_the_text_chart(self):
        path = self.write_plan(SPEC_EXAMPLE)
        _, out, _ = run([path, "--load", "--bar", "5"])
        self.assertIn("█" * 5, out)

    def test_load_flag_adds_chart_to_text_output(self):
        path = self.write_plan(SPEC_EXAMPLE)
        code, out, _ = run([path, "--load"])
        self.assertEqual(code, 0)
        self.assertIn("load (scheduled minutes per hour)", out)
        self.assertIn("busiest hour: 55 min", out)

    def test_bar_below_one_is_a_usage_error(self):
        path = self.write_plan(SPEC_EXAMPLE)
        code, _, err = run([path, "--bar", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--bar", err)


class StrictTests(TempFileMixin):
    def test_strict_turns_warnings_into_exit_1(self):
        path = self.write_plan(SPEC_EXAMPLE)
        code, out, err = run([path, "--strict"])
        self.assertEqual(code, 1)
        self.assertTrue(out)  # the schedule is still printed
        self.assertTrue(err)

    def test_strict_is_silent_when_no_warnings(self):
        clean = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T01:00:00+00:00"},
            "jobs": [{"id": "a", "duration_min": 30}],
        }
        code, _, _ = run([self.write_plan(clean), "--strict"])
        self.assertEqual(code, 0)


class FailureExitTests(TempFileMixin):
    def test_all_jobs_dropped_exits_1(self):
        document = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T00:30:00+00:00"},
            "jobs": [{"id": "big", "duration_min": 60}],
        }
        code, out, err = run([self.write_plan(document)])
        self.assertEqual(code, 1)
        self.assertIn("DROPPED id=big run=1 reason=no-slot", err)

    def test_all_jobs_blocked_exits_1(self):
        document = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T01:00:00+00:00"},
            "jobs": [
                {"id": "doomed", "duration_min": 120},
                {"id": "child", "duration_min": 10, "deps": ["doomed"]},
            ],
        }
        code, _, err = run([self.write_plan(document)])
        self.assertEqual(code, 1)
        self.assertIn("DROPPED id=doomed", err)
        self.assertIn("BLOCKED id=child reason=dep-missing:doomed", err)

    def test_partial_drop_still_exits_0(self):
        document = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T01:00:00+00:00"},
            "jobs": [
                {"id": "ok", "duration_min": 30},
                {"id": "doomed", "duration_min": 120},
            ],
        }
        code, out, _ = run([self.write_plan(document)])
        self.assertEqual(code, 0)
        self.assertIn("ok", out)


class UsageErrorTests(TempFileMixin):
    def assert_usage(self, argv, document=None, stdin_text=None, fragment=None):
        code, _, err = run(argv, document=document, stdin_text=stdin_text)
        self.assertEqual(code, 2, err)
        self.assertIn("kaynakplanlar: error:", err)
        if fragment:
            self.assertIn(fragment, err)
        return err

    def test_cycle_names_the_cycle(self):
        document = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-02T00:00:00+00:00"},
            "jobs": [
                {"id": "a", "duration_min": 5, "deps": ["b"]},
                {"id": "b", "duration_min": 5, "deps": ["a"]},
            ],
        }
        self.assert_usage([self.write_plan(document)], fragment="a -> b -> a")

    def test_unknown_dependency_names_job_and_dep(self):
        document = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-02T00:00:00+00:00"},
            "jobs": [{"id": "a", "duration_min": 5, "deps": ["ghost"]}],
        }
        self.assert_usage([self.write_plan(document)], fragment="ghost")

    def test_bad_job_names_the_job(self):
        document = {
            "window": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-02T00:00:00+00:00"},
            "jobs": [{"id": "badjob", "duration_min": 0}],
        }
        self.assert_usage([self.write_plan(document)], fragment="badjob")

    def test_bad_window_is_a_usage_error(self):
        document = {
            "window": {"start": "2026-10-02T00:00:00+00:00", "end": "2026-10-01T00:00:00+00:00"},
            "jobs": [],
        }
        self.assert_usage([self.write_plan(document)])

    def test_malformed_json_is_a_usage_error(self):
        self.assert_usage([], stdin_text="{not json")

    def test_empty_stdin_is_a_usage_error(self):
        self.assert_usage([], stdin_text="")

    def test_missing_file_is_a_usage_error(self):
        self.assert_usage(["/nonexistent/plan.json"], fragment="cannot read")

    def test_bad_flag_is_a_usage_error(self):
        code, _, err = run(["--no-such-flag"])
        self.assertEqual(code, 2)
        self.assertIn("error", err.lower())

    def test_non_object_document(self):
        self.assert_usage([], stdin_text="[]")


class VersionTests(unittest.TestCase):
    def test_version_flag(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("kaynakplanlar "))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
