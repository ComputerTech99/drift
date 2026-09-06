#!/usr/bin/env python3
"""Tests for drift.py: complete input, redacted transcript/graph fields, missing
fields, and local-backend-only operation with no network. Stdlib only
(unittest + unittest.mock) - no fixtures beyond what's defined here, since none
shipped with the repo; these stand in as the partial-analysis fixture."""
import contextlib
import io
import json
import sys
import unittest
from unittest import mock

import drift

COMPLETE_TRANSCRIPT = "\n".join(json.dumps(r) for r in [
    {"type": "user", "message": {"role": "user", "id": "m1", "content": "add a timeout"}},
    {"type": "assistant", "message": {"role": "assistant", "id": "m2", "content": [
        {"type": "text", "text": "adding it"},
        {"type": "tool_use", "name": "Write", "input": {"content": "def call_with_retry(): pass"}},
    ]}},
])

# Sensitive fields nulled in place, as a redactor would do - keys stay present.
REDACTED_TRANSCRIPT = "\n".join(json.dumps(r) for r in [
    {"type": "user", "message": {"role": "user", "id": "m1", "content": None}},
    {"type": "assistant", "message": None},
    {"type": "assistant", "message": {"role": "assistant", "id": "m3", "content": [
        {"type": "tool_use", "name": "Write", "input": None},
    ]}},
])

MISSING_FIELDS_TRANSCRIPT = "\n".join([
    json.dumps({"foo": "bar"}),
    json.dumps({"role": "user"}),
    "not valid json{{{",
    "null",
])

COMPLETE_GRAPH = "\n".join(json.dumps(r) for r in [
    {"record_type": "symbol", "id": "s1", "name": "call_with_retry", "qualified_name": "call_with_retry",
     "file_path": "retry.py", "start_line": 1, "end_line": 1},
    {"record_type": "symbol", "id": "s2", "name": "test_it", "qualified_name": "test_it",
     "file_path": "tests/test_retry.py", "start_line": 1, "end_line": 1},
    {"record_type": "relation", "type": "CALLS", "from_id": "s2", "to_id": "s1"},
])

PARTIAL_GRAPH = "\n".join([
    json.dumps({"record_type": "symbol", "id": None, "name": None, "file_path": None}),
    json.dumps({"record_type": "relation", "type": "CALLS", "from_id": "s2"}),
    json.dumps({"record_type": "symbol", "id": "s1", "name": "call_with_retry"}),
    "null",
])


class TestParseTranscript(unittest.TestCase):
    def test_complete_groups_turns(self):
        turns = drift.parse_transcript(COMPLETE_TRANSCRIPT)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["kind"], "prompt")
        self.assertEqual(turns[0]["text"], "add a timeout")
        self.assertEqual(turns[1]["tool_uses"][0]["name"], "Write")

    def test_redacted_fields_degrade_not_crash(self):
        turns = drift.parse_transcript(REDACTED_TRANSCRIPT)  # must not raise
        for turn in turns:
            for tool_use in turn["tool_uses"]:
                self.assertEqual(tool_use["input"], {})  # nulled input degrades to {}, never None

    def test_missing_and_malformed_lines_do_not_crash(self):
        # only {"role": "user"} is usable; the rest (no role, invalid JSON, a
        # bare `null`) must be skipped, not raise.
        turns = drift.parse_transcript(MISSING_FIELDS_TRANSCRIPT)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "")


class TestBuildGraphIndex(unittest.TestCase):
    def test_complete_graph(self):
        graph = drift.build_graph_index(COMPLETE_GRAPH)
        self.assertIn("call_with_retry", graph["by_name"])
        self.assertEqual(len(graph["edges_by_source"]["s2"]), 1)

    def test_partial_graph_does_not_crash_and_keeps_usable_symbols(self):
        graph = drift.build_graph_index(PARTIAL_GRAPH)  # must not raise
        self.assertIn("call_with_retry", graph["by_name"])


class TestFindDroppedTurn(unittest.TestCase):
    def test_redacted_tool_input_does_not_crash(self):
        turns = drift.parse_transcript(REDACTED_TRANSCRIPT)
        self.assertIsNone(drift.find_dropped_turn(turns, ["nonexistent_symbol"]))


class TestEvalLiteralInBody(unittest.TestCase):
    def test_missing_file_returns_false_not_crash(self):
        graph = drift.build_graph_index(json.dumps(
            {"record_type": "symbol", "id": "s1", "name": "f", "file_path": "does/not/exist.py",
             "start_line": 1, "end_line": 2}))
        graph["repo"] = "/nonexistent/repo/path"
        self.assertFalse(drift.eval_literal_in_body({"symbol": "f", "value": "x"}, graph))

    def test_no_value_is_none_not_false(self):
        graph = {"by_name": {}, "by_id": {}, "edges_by_source": {}, "repo": "."}
        self.assertIsNone(drift.eval_literal_in_body({"symbol": "f", "value": ""}, graph))


class TestEvidenceTiering(unittest.TestCase):
    def test_graph_relation_lookups_are_confirmed_structural(self):
        for t in ("symbol_exists", "has_inbound_edge", "calls"):
            self.assertEqual(drift.ASSERTION_HANDLERS[t][3], "confirmed_structural")

    def test_regex_and_path_heuristics_are_heuristic(self):
        for t in ("test_references", "literal_in_body"):
            self.assertEqual(drift.ASSERTION_HANDLERS[t][3], "heuristic")


class TestBackendGating(unittest.TestCase):
    @mock.patch("drift.run_entire")
    @mock.patch("drift.extract_requirements")
    def test_api_without_allow_external_exits_before_any_call(self, mock_extract, mock_run):
        argv = ["drift.py", "--repo", ".", "--checkpoint", "c1", "--backend", "api"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit):
                drift.main()
        mock_run.assert_not_called()
        mock_extract.assert_not_called()

    @mock.patch("requests.post")
    def test_local_backend_only_ever_contacts_localhost(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"message": {"content": "[]"}}
        drift.extract_requirements("do something", backend="local")
        self.assertTrue(mock_post.call_args[0][0].startswith("http://localhost"))


class TestMainEndToEnd(unittest.TestCase):
    def _run_main(self, transcript, graph_ndjson, requirements, extra_argv=()):
        def fake_run_entire(cmd_args, repo):
            return transcript if cmd_args[0] == "checkpoint" else graph_ndjson
        argv = ["drift.py", "--repo", ".", "--checkpoint", "c1", *extra_argv]
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("drift.run_entire", side_effect=fake_run_entire), \
             mock.patch("drift.extract_requirements", return_value=requirements), \
             contextlib.redirect_stdout(buf):
            try:
                drift.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return buf.getvalue(), code

    def test_complete_input_surfaces_all_tiers(self):
        reqs = [
            {"requirement": "add call_with_retry", "assertion": {"type": "symbol_exists", "args": {"pattern": "call_with_retry"}}},
            {"requirement": "test it", "assertion": {"type": "test_references", "args": {"symbol": "call_with_retry"}}},
            {"requirement": "single file, drift.py", "assertion": {"type": "unverifiable", "args": {}}},
        ]
        out, code = self._run_main(COMPLETE_TRANSCRIPT, COMPLETE_GRAPH, reqs)
        self.assertIn("evidence_class", out)
        self.assertIn("confirmed_structural", out)
        self.assertIn("heuristic", out)
        self.assertIn("requires_verification", out)
        self.assertIn("unverified", out)  # admitted gap stays a visible row, never silently dropped
        self.assertEqual(code, 0)

    def test_redacted_and_missing_fields_yield_partial_not_crashed_report(self):
        reqs = [{"requirement": "add call_with_retry",
                 "assertion": {"type": "symbol_exists", "args": {"pattern": "call_with_retry"}}}]
        out, code = self._run_main(REDACTED_TRANSCRIPT, PARTIAL_GRAPH, reqs)  # must not raise
        self.assertIn("call_with_retry", out)
        self.assertIn("landed", out)


if __name__ == "__main__":
    unittest.main()
