"""Draft model calls are written down, both sides, in full.

`wes_server` logs every voice and Discord exchange and the Grafana table shows
them verbatim. The draft agents POST straight to Ollama, so a whole draft used
to leave behind one `reason` sentence per pick and nothing about the shortlist
that produced it — every suspect pick and every fabricated chat line had to be
reconstructed afterwards against the API.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_draft_log  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(wes_draft_log, "LOG", str(tmp_path / "draft.jsonl"))


class TestItRecordsBothSides:
    def test_the_whole_payload_is_kept(self):
        """Summarising the payload would recreate the problem: the shortlist
        the model saw, in the order it saw it, IS the question when a pick
        looks wrong."""
        payload = {"shortlist": [{"name": "Jahmyr Gibbs", "vor": 5.37},
                                 {"name": "Puka Nacua", "vor": 6.34}]}
        wes_draft_log.log_call("draft.pick", payload, '{"candidate": "x"}',
                               1.5, model="gemma4:12b")
        rec = wes_draft_log.recent(1)[0]
        assert "Jahmyr Gibbs" in rec["transcript"]
        assert "Puka Nacua" in rec["transcript"]
        assert rec["reply"] == '{"candidate": "x"}'
        assert rec["seconds"] == 1.5 and rec["model"] == "gemma4:12b"

    def test_it_matches_the_server_record_shape(self):
        """Same keys as wes_server.log_turn, so the existing Grafana table
        renders draft calls with no dashboard changes."""
        wes_draft_log.log_call("draft.pick", {"a": 1}, "reply")
        rec = wes_draft_log.recent(1)[0]
        for key in ("ts", "channel", "transcript", "reply", "tools",
                    "escalated"):
            assert key in rec, key

    def test_a_failed_call_is_recorded_not_dropped(self):
        """A call that returned nothing, or unparseable JSON, is exactly the
        turn worth reading afterwards."""
        wes_draft_log.log_call("draft.pick", {"a": 1}, None,
                               error="JSONDecodeError: boom")
        rec = wes_draft_log.recent(1)[0]
        assert rec["error"] == "JSONDecodeError: boom"

    def test_an_empty_reply_is_tagged(self):
        wes_draft_log.log_call("draft.banter", {"a": 1}, "")
        assert wes_draft_log.recent(1)[0]["error"] == "empty_reply"

    def test_kinds_are_filterable(self):
        wes_draft_log.log_call("draft.pick", {}, "a")
        wes_draft_log.log_call("draft.banter", {}, "b")
        assert len(wes_draft_log.recent(10)) == 2
        got = wes_draft_log.recent(10, kind="draft.banter")
        assert len(got) == 1 and got[0]["reply"] == "b"

    def test_newest_first(self):
        for i in range(3):
            wes_draft_log.log_call("draft.pick", {}, f"r{i}")
        assert [r["reply"] for r in wes_draft_log.recent(3)] == \
            ["r2", "r1", "r0"]


class TestItCannotBreakADraft:
    def test_an_unwritable_path_does_not_raise(self, monkeypatch):
        """A logger that can break a draft is worse than no logger."""
        monkeypatch.setattr(wes_draft_log, "LOG",
                            "Z:\\no\\such\\dir\\x.jsonl")
        wes_draft_log.log_call("draft.pick", {"a": 1}, "reply")

    def test_an_unserialisable_payload_still_records(self):
        wes_draft_log.log_call("draft.pick", {"obj": object()}, "reply")
        assert wes_draft_log.recent(1), "record was lost"

    def test_a_missing_file_reads_as_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wes_draft_log, "LOG", str(tmp_path / "none.jsonl"))
        assert wes_draft_log.recent(5) == []

    def test_a_corrupt_line_is_skipped(self, tmp_path, monkeypatch):
        p = tmp_path / "c.jsonl"
        monkeypatch.setattr(wes_draft_log, "LOG", str(p))
        wes_draft_log.log_call("draft.pick", {}, "good")
        with open(p, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        got = wes_draft_log.recent(5)
        assert len(got) == 1 and got[0]["reply"] == "good"


class TestTheAgentsActuallyCallIt:
    def test_a_pick_call_is_logged_with_its_payload(self, monkeypatch):
        import wes_draft_agent
        monkeypatch.setattr(wes_draft_agent, "wes_draft_log", wes_draft_log)
        wes_draft_agent._ask_model(
            {"shortlist": [{"name": "Bijan Robinson"}]},
            _post_fn=lambda _b: '{"candidate": "Bijan Robinson"}')
        rec = wes_draft_log.recent(1)[0]
        assert rec["channel"] == "draft.pick"
        assert "Bijan Robinson" in rec["transcript"]

    def test_a_banter_call_is_logged(self, monkeypatch):
        import wes_banter
        wes_banter._ask({"draft": {"round": 1}},
                        _post_fn=lambda _b: '{"message": "hi"}')
        rec = wes_draft_log.recent(1)[0]
        assert rec["channel"] == "draft.banter"
        assert "round" in rec["transcript"]

    def test_a_dropped_line_is_logged_with_the_context_that_made_it(self):
        """The most useful record this produces: a fabrication caught with the
        exact payload that produced it still attached."""
        import wes_banter
        bt = wes_banter.Banter("D", me="us", mode="auto", min_gap_s=0,
                               _now=lambda: 1000.0)
        ctx = {"recent_picks": [{"pick": 39, "player": "Brock Bowers",
                                 "round": 7, "ours": False, "by": "slot 4",
                                 "verdict": "steal"}]}
        bt.tick(context={"recent_picks": []}, _read_fn=lambda _d: [],
                _send_fn=lambda _d, ln: True,
                _post_fn=lambda _b: '{"message": "x"}')            # prime
        act, _ = bt.tick(
            context=ctx, _read_fn=lambda _d: [],
            _send_fn=lambda _d, ln: True,
            _post_fn=lambda _b: '{"message": "Brock Bowers at 7, wow"}')
        assert act == "dropped"
        got = wes_draft_log.recent(5, kind="draft.banter.dropped")
        assert got and "39" in got[0]["error"]
        assert "Brock Bowers" in got[0]["transcript"]
