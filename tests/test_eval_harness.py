"""Unit tests for eval_turns.py's pure logic (no server, no API, no audio).

    C:\\Users\\awarm\\wes-pc\\.venv\\Scripts\\python.exe -m pytest
        Z:\\wes\\tests\\test_eval_harness.py -q
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_turns as ev  # noqa: E402


class TestParseJudge:
    def test_clean_json(self):
        s = ev.parse_judge(
            '{"correct": 2, "concise": 1, "natural": 2, '
            '"hallucination": false, "note": "fine"}')
        assert s == {"judge_correct": 2, "judge_concise": 1,
                     "judge_natural": 2, "hallucination": 0,
                     "judge_note": "fine"}

    def test_json_wrapped_in_prose_and_fences(self):
        s = ev.parse_judge(
            'Here is my grade:\n```json\n{"correct": 0, "concise": 2, '
            '"natural": 1, "hallucination": true, "note": "made up a fact"}'
            '\n```')
        assert s["judge_correct"] == 0
        assert s["hallucination"] == 1

    def test_garbage_returns_none(self):
        assert ev.parse_judge("I refuse to grade this.") is None
        assert ev.parse_judge('{"correct": "excellent"}') is None
        assert ev.parse_judge('{"concise": 1}') is None  # missing keys

    def test_long_note_truncated(self):
        s = ev.parse_judge(
            '{"correct": 1, "concise": 1, "natural": 1, '
            '"hallucination": false, "note": "' + "x" * 500 + '"}')
        assert len(s["judge_note"]) == 200


class TestJudgeGate:
    def test_no_history_never_fails(self):
        assert ev.judge_gate(0.0, []) is False
        assert ev.judge_gate(None, [1.8, 1.9]) is False

    def test_small_drop_passes(self):
        assert ev.judge_gate(1.6, [1.8, 1.9, 1.8]) is False

    def test_big_drop_fails(self):
        assert ev.judge_gate(1.4, [1.8, 1.9, 1.8]) is True

    def test_median_uses_last_n_runs_only(self):
        # old bad runs fall out of the window: median of last 5 is 1.8
        prior = [0.5, 0.5, 1.8, 1.8, 1.8, 1.8, 1.8]
        assert ev.judge_gate(1.4, prior) is True


class TestHistorySchema:
    def _with_history(self, tmp_path, monkeypatch, header, rows):
        path = str(tmp_path / "hist.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        monkeypatch.setattr(ev, "HISTORY", path)
        return path

    def test_old_header_migrates_and_appends(self, tmp_path, monkeypatch):
        old = ["ts", "case", "passed", "fails", "transcript", "reply", "spec",
               "stt_ms", "ttfa_ms", "total_ms", "audio_s"]
        path = self._with_history(
            tmp_path, monkeypatch, old,
            [["t1", "time-basic", "1", "", "what time", "noon", "0",
              "300", "1200", "3000", "2.0"]])
        ev.append_history([{k: "" for k in ev.FIELDS} |
                           {"ts": "t2", "case": "time-basic", "passed": "1",
                            "judge_correct": 2}])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert [r["ts"] for r in rows] == ["t1", "t2"]
        assert rows[0]["judge_correct"] == ""      # padded old row
        assert rows[1]["judge_correct"] == "2"
        assert rows[0]["reply"] == "noon"          # old data intact

    def test_prior_judge_averages_groups_by_run(self, tmp_path, monkeypatch):
        self._with_history(
            tmp_path, monkeypatch, ev.FIELDS,
            [["t1", "a", "1", "", "", "", "0", "1", "1", "1", "1",
              "haiku", "2", "2", "2", "0", ""],
             ["t1", "b", "1", "", "", "", "0", "1", "1", "1", "1",
              "haiku", "1", "2", "2", "0", ""],
             ["t2", "a", "1", "", "", "", "0", "1", "1", "1", "1",
              "", "", "", "", "", ""],   # unjudged run: excluded
             ["t3", "a", "1", "", "", "", "0", "1", "1", "1", "1",
              "haiku", "1", "1", "1", "0", ""]])
        assert ev.prior_judge_averages("haiku") == [1.5, 1.0]

    def test_prior_judge_averages_separates_backends(self, tmp_path,
                                                     monkeypatch):
        # haiku and local runs must never share a baseline; rows from before
        # the judge column existed (blank backend) count as haiku
        old = [f for f in ev.FIELDS if f != "judge"]
        self._with_history(
            tmp_path, monkeypatch, old,
            [["t0", "a", "1", "", "", "", "0", "1", "1", "1", "1",
              "2", "2", "2", "0", ""]])
        ev.append_history([  # migrates, then adds a local-judged run
            {k: "" for k in ev.FIELDS} |
            {"ts": "t1", "case": "a", "passed": "1",
             "judge": "local", "judge_correct": 1}])
        assert ev.prior_judge_averages("haiku") == [2.0]
        assert ev.prior_judge_averages("local") == [1.0]

    def test_local_judge_call_shape_and_parse(self, monkeypatch):
        # network-free: capture the Ollama request, return a canned grade
        captured = {}

        class Resp:
            def read(self):
                return json.dumps({"message": {"content":
                    '{"note": "fine", "correct": 2, "concise": 1, '
                    '"natural": 2, "hallucination": false}'}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return Resp()

        monkeypatch.setattr(ev.urllib.request, "urlopen", fake_urlopen)
        scores = ev.judge_case(
            {"judge": "Is it Paris?"}, "capital of France?", "Paris.", "local")
        assert scores == {"judge_correct": 2, "judge_concise": 1,
                          "judge_natural": 2, "hallucination": 0,
                          "judge_note": "fine"}
        assert captured["url"] == ev.OLLAMA_URL + "/api/chat"
        body = captured["body"]
        assert body["model"] == ev.JUDGE_LOCAL_MODEL
        assert body["format"] == "json"            # parseable output
        assert body["options"]["temperature"] == 0  # repeatable grades
        assert body["messages"][0]["content"] == ev.JUDGE_SYSTEM
        assert "Is it Paris?" in body["messages"][1]["content"]

    def test_previous_results_reads_last_run(self, tmp_path, monkeypatch):
        self._with_history(
            tmp_path, monkeypatch, ev.FIELDS,
            [["t1", "a", "1"] + [""] * (len(ev.FIELDS) - 3),
             ["t2", "a", "0"] + [""] * (len(ev.FIELDS) - 3),
             ["t2", "b", "1"] + [""] * (len(ev.FIELDS) - 3)])
        assert ev.previous_results() == {"a": False, "b": True}
