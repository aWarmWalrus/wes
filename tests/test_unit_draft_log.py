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

from sleeper import draft_log as wes_draft_log  # noqa: E402


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


class TestTheEndpointIsSeparateFromTurns:
    """Its own endpoint and table, deliberately. Merging draft calls into
    /turns was the first attempt and wrong twice: the two have different
    shapes, and it made /turns read a second global path — which promptly fed
    four turn-log tests the owner's real draft log."""

    def test_draft_turns_serves_the_draft_log(self):
        import wes_server as ws
        wes_draft_log.log_call("draft.pick", {"shortlist": ["Gibbs"]}, "ok",
                               1.2, model="gemma4:12b")
        with ws.app.test_client() as c:
            got = c.get("/draft_turns").get_json()["turns"]
        assert len(got) == 1
        assert got[0]["channel"] == "draft.pick"
        assert "Gibbs" in got[0]["transcript"]

    def test_turns_does_NOT_include_draft_calls(self):
        """The coupling that broke four tests must not come back."""
        import wes_server as ws
        wes_draft_log.log_call("draft.pick", {"a": 1}, "ok")
        ws.record_turn("hello", "hi there", channel="voice")
        with ws.app.test_client() as c:
            turns = c.get("/turns").get_json()["turns"]
        assert [t["channel"] for t in turns] == ["voice"]

    def test_it_filters_by_kind(self):
        import wes_server as ws
        wes_draft_log.log_call("draft.pick", {}, "a")
        wes_draft_log.log_call("draft.banter", {}, "b")
        with ws.app.test_client() as c:
            got = c.get("/draft_turns?kind=draft.banter").get_json()["turns"]
        assert len(got) == 1 and got[0]["reply"] == "b"

    def test_pretty_indents_the_payload_for_reading(self):
        """One line is right for the file and wrong for a person: a shortlist
        of eight players is unreadable until it is indented."""
        import wes_server as ws
        wes_draft_log.log_call("draft.pick",
                               {"shortlist": [{"name": "Gibbs", "vor": 5.4}]},
                               '{"candidate": "Gibbs"}', 1.0)
        with ws.app.test_client() as c:
            plain = c.get("/draft_turns").get_json()["turns"][0]
            nice = c.get("/draft_turns?pretty=1").get_json()["turns"][0]
        assert "\n" not in plain["transcript"], "raw view should stay compact"
        assert "\n  " in nice["transcript"], "pretty view should be indented"
        assert '"name": "Gibbs"' in nice["transcript"]

    def test_pretty_adds_a_readable_detail_blob(self):
        import wes_server as ws
        wes_draft_log.log_call("draft.banter.dropped", {"recent_picks": []},
                               "Bowers at 7", error="says Bowers went at 7")
        with ws.app.test_client() as c:
            rec = c.get("/draft_turns?pretty=1").get_json()["turns"][0]
        assert "draft.banter.dropped" in rec["detail"]
        assert "says Bowers went at 7" in rec["detail"]
        assert "--- payload sent ---" in rec["detail"]
        assert "--- model reply ---" in rec["detail"]

    def test_the_header_omits_fields_a_record_does_not_have(self):
        """Outcome records time no call and name no model. A header reading
        "Nones" is the small ugliness that makes a panel feel untrustworthy."""
        import wes_server as ws
        wes_draft_log.log_call("draft.banter.said", {"a": 1}, "nice one",
                               reacting_to="pick 9 slot 4 took Dowdle")
        with ws.app.test_client() as c:
            rec = c.get("/draft_turns?pretty=1").get_json()["turns"][0]
        assert "None" not in rec["detail"]
        assert "reacting to: pick 9" in rec["detail"]

    def test_pretty_leaves_non_json_alone(self):
        """The reply is raw model output and is not always valid JSON — that
        is exactly the case worth reading, so it must not be dropped."""
        import wes_server as ws
        wes_draft_log.log_call("draft.pick", {"a": 1}, "not json at all")
        with ws.app.test_client() as c:
            rec = c.get("/draft_turns?pretty=1").get_json()["turns"][0]
        assert rec["reply"] == "not json at all"

    def test_a_missing_draft_log_is_an_empty_list_not_a_500(self, monkeypatch,
                                                            tmp_path):
        import wes_server as ws
        monkeypatch.setattr(wes_draft_log, "LOG", str(tmp_path / "gone.jsonl"))
        with ws.app.test_client() as c:
            r = c.get("/draft_turns")
        assert r.status_code == 200 and r.get_json()["turns"] == []


class TestTheAgentsActuallyCallIt:
    def test_a_pick_call_is_logged_with_its_payload(self, monkeypatch):
        from sleeper import agent as wes_draft_agent
        monkeypatch.setattr(wes_draft_agent, "wes_draft_log", wes_draft_log)
        wes_draft_agent._ask_model(
            {"shortlist": [{"name": "Bijan Robinson"}]},
            _post_fn=lambda _b: '{"candidate": "Bijan Robinson"}')
        rec = wes_draft_log.recent(1)[0]
        assert rec["channel"] == "draft.pick"
        assert "Bijan Robinson" in rec["transcript"]

    def test_a_banter_call_is_logged(self, monkeypatch):
        from sleeper import banter as wes_banter
        wes_banter._ask({"draft": {"round": 1}},
                        _post_fn=lambda _b: '{"message": "hi"}')
        rec = wes_draft_log.recent(1)[0]
        assert rec["channel"] == "draft.banter"
        assert "round" in rec["transcript"]

    def test_a_posted_line_is_logged_too(self):
        """Logging only rejections answered "did it make something up" and left
        the commoner question -- did this reach the room -- unrecorded. A line
        composed and rate-limited looked exactly like one that was posted."""
        from sleeper import banter as wes_banter
        bt = wes_banter.Banter("D", me="us", mode="auto", min_gap_s=0,
                               _now=lambda: 1000.0)
        ctx = {"recent_picks": [{"pick": 9, "player": "Rico Dowdle",
                                 "round": 2, "ours": False, "by": "slot 4",
                                 "we_wanted": True, "our_rank_for_him": 3}]}
        bt.tick(context={"recent_picks": []}, _read_fn=lambda _d: [],
                _send_fn=lambda _d, ln: True,
                _post_fn=lambda _b: '{"message": "x"}')            # prime
        act, _ = bt.tick(
            context=ctx, _read_fn=lambda _d: [],
            _send_fn=lambda _d, ln: True,
            _post_fn=lambda _b: '{"message": "Aw man, Rico Dowdle."}')
        assert act == "said"
        got = wes_draft_log.recent(5, kind="draft.banter.said")
        assert got and got[0]["reply"] == "Aw man, Rico Dowdle."
        assert "Rico Dowdle" in got[0]["reacting_to"]
        assert got[0]["mode"] == "auto"

    def test_the_outcome_record_carries_the_chat_the_model_saw(self):
        """The outcome used to log the draft context alone while the model was
        given context AND chat, so a dropped line was filed without the
        messages that provoked it -- half its cause missing."""
        from sleeper import banter as wes_banter
        bt = wes_banter.Banter("D", me="us", mode="auto", min_gap_s=0,
                               _now=lambda: 1000.0)
        msgs = [{"author": "GMSnappy", "text": "your roster is a mess",
                 "system": False}]
        bt.tick(context={"recent_picks": []}, _read_fn=lambda _d: [],
                _send_fn=lambda _d, ln: True,
                _post_fn=lambda _b: '{"message": "x"}')            # prime
        bt.tick(context={"recent_picks": []}, _read_fn=lambda _d: msgs,
                _send_fn=lambda _d, ln: True,
                _post_fn=lambda _b: '{"message": "rude."}')
        rec = wes_draft_log.recent(1, kind="draft.banter.said")[0]
        payload = json.loads(rec["transcript"])
        assert "recent_chat" in payload, "chat missing from the outcome record"
        assert payload["recent_chat"][0]["said"] == "your roster is a mess"
        assert "draft" in payload

    def test_the_logged_payload_matches_what_compose_sends(self):
        """One construction of "the payload", shared. If it drifts the log
        stops describing the call it claims to."""
        from sleeper import banter as wes_banter
        msgs = [{"author": "x", "text": "hi", "system": False}]
        ctx = {"round": 3}
        assert wes_banter.build_payload(msgs, ctx) == {
            "draft": ctx,
            "recent_chat": [{"from": "x", "said": "hi"}]}

    def test_propose_mode_records_that_nothing_was_sent(self):
        from sleeper import banter as wes_banter
        bt = wes_banter.Banter("D", me="us", mode="propose", min_gap_s=0,
                               _now=lambda: 1000.0)
        sent = []
        ctx = {"recent_picks": [{"pick": 9, "player": "Rico Dowdle",
                                 "round": 2, "ours": False, "by": "slot 4",
                                 "verdict": "steal"}]}
        bt.tick(context={"recent_picks": []}, _read_fn=lambda _d: [],
                _post_fn=lambda _b: '{"message": "x"}')            # prime
        act, _ = bt.tick(
            context=ctx, _read_fn=lambda _d: [],
            _send_fn=lambda _d, ln: sent.append(ln) or True,
            _post_fn=lambda _b: '{"message": "Nice one."}')
        assert act == "would_say" and sent == []
        assert wes_draft_log.recent(5, kind="draft.banter.would_say")

    def test_a_dropped_line_is_logged_with_the_context_that_made_it(self):
        """The most useful record this produces: a fabrication caught with the
        exact payload that produced it still attached."""
        from sleeper import banter as wes_banter
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


class TestTheEndpointEmitsAUniformSchema:
    """Every row carries every key, whatever the record actually held.

    The draft log is deliberately heterogeneous -- a `draft.pick` has a model
    and a latency, a `draft.banter.said` outcome record has neither -- which is
    right for a file and wrong for a table. Grafana's Infinity datasource
    selects columns BY NAME and types them, so a `type: string` column whose key
    is missing resolves to null and the whole panel dies with

        TypeError: Cannot read properties of null (reading 'length')

    naming neither the column nor the row. Seen live 2026-09-03 on the "Draft
    agent calls" panel; on the real log `error` was absent from 24 of 25 rows
    and `model` from 9.
    """

    # The columns observability/dashboards/wes-overview.json selects as strings.
    STRING_COLUMNS = ("ts", "channel", "model", "transcript", "reply", "error")

    def test_missing_text_fields_come_back_as_empty_strings(self):
        import wes_server as ws
        # A banter outcome record: no model, no seconds, no error.
        wes_draft_log.log_call("draft.banter.said", {"draft": {}}, "nice pick",
                               mode="auto", reacting_to="pick 12")
        with ws.app.test_client() as c:
            row = c.get("/draft_turns").get_json()["turns"][0]
        for col in self.STRING_COLUMNS:
            assert col in row, f"{col} missing entirely"
            assert row[col] is not None, f"{col} is null -- the panel crashes"
            assert isinstance(row[col], str)

    def test_every_row_has_the_same_keys_even_when_kinds_differ(self):
        import wes_server as ws
        wes_draft_log.log_call("draft.pick", {"shortlist": []}, "ok", 1.2,
                               model="gemma4:12b")
        wes_draft_log.log_call("draft.banter.said", {}, "ha", mode="auto")
        wes_draft_log.log_call("draft.banter.dropped", {}, "no",
                               error="fabricated a pick number")
        with ws.app.test_client() as c:
            rows = c.get("/draft_turns").get_json()["turns"]
        assert len(rows) == 3
        for col in self.STRING_COLUMNS:
            assert all(col in r and r[col] is not None for r in rows), col

    def test_seconds_stays_null_rather_than_becoming_zero(self):
        """A numeric column renders null as blank, which is honest. 0.0 would
        claim the call took no time, which is a different and false statement."""
        import wes_server as ws
        wes_draft_log.log_call("draft.banter.said", {}, "hi")   # times no call
        wes_draft_log.log_call("draft.pick", {}, "ok", 2.5)
        with ws.app.test_client() as c:
            rows = c.get("/draft_turns").get_json()["turns"]
        by_kind = {r["channel"]: r for r in rows}
        assert by_kind["draft.banter.said"]["seconds"] is None
        assert by_kind["draft.pick"]["seconds"] == 2.5

    def test_real_values_are_never_clobbered(self):
        import wes_server as ws
        wes_draft_log.log_call("draft.pick", {"shortlist": ["Gibbs"]},
                               "took Gibbs", 3.3, model="gemma4:12b")
        with ws.app.test_client() as c:
            row = c.get("/draft_turns").get_json()["turns"][0]
        assert row["model"] == "gemma4:12b"
        assert row["seconds"] == 3.3
        assert "Gibbs" in row["transcript"] and row["reply"] == "took Gibbs"
        assert row["error"] == ""      # genuinely absent -> blank, not null

    def test_pretty_view_is_uniform_too(self):
        """?pretty=1 feeds the 'Latest call in full' panel, which selects
        `detail` as a string -- same crash if a row lacks it."""
        import wes_server as ws
        wes_draft_log.log_call("draft.banter.said", {}, "hi", mode="auto")
        wes_draft_log.log_call("draft.pick", {}, "ok", 1.0, model="m")
        with ws.app.test_client() as c:
            rows = c.get("/draft_turns?pretty=1").get_json()["turns"]
        assert all(isinstance(r.get("detail"), str) and r["detail"] for r in rows)


class TestTimingBreakdown:
    """Wall time alone hid a real problem for an afternoon.

    A draft showed picks at 61s, 53s and 67s against a 120s clock; replaying
    those exact payloads afterwards took 8.1s, 3.3s and 4.6s. Ollama serialises
    requests, so a call can do three seconds of work and spend forty waiting
    behind another -- and one Ollama serves this whole machine. `queued_s`
    separates "the model is slow" from "the model never got a turn".
    """

    def test_derives_queue_time_from_the_ollama_durations(self):
        ns = 10 ** 9
        got = wes_draft_log.timings(
            {"load_duration": 1 * ns, "prompt_eval_duration": 2 * ns,
             "eval_duration": 3 * ns, "prompt_eval_count": 610,
             "eval_count": 85},
            wall=40.0)
        assert got["load_s"] == 1.0
        assert got["prompt_s"] == 2.0
        assert got["gen_s"] == 3.0
        assert got["prompt_tok"] == 610 and got["gen_tok"] == 85
        assert got["queued_s"] == 34.0, "40s wall, 6s of work -> 34s queued"

    def test_a_fast_call_never_reports_negative_queueing(self):
        """The durations come from the server and the wall from the client, so
        a fast call can round to a hair below the sum. Negative queue time would
        read as a measurement bug rather than a fast call."""
        ns = 10 ** 9
        got = wes_draft_log.timings(
            {"load_duration": 0, "prompt_eval_duration": ns,
             "eval_duration": 2 * ns}, wall=2.99)
        assert got["queued_s"] == 0.0

    def test_missing_durations_are_zero_not_a_crash(self):
        got = wes_draft_log.timings({"eval_count": 12}, wall=5.0)
        assert got["gen_s"] == 0.0 and got["queued_s"] == 5.0
        assert got["gen_tok"] == 12 and got["prompt_tok"] is None

    def test_a_non_dict_response_yields_nothing(self):
        """Tests inject a plain string through `_post_fn`; a logger must not
        care, and must not invent numbers it does not have."""
        assert wes_draft_log.timings("just a string", wall=1.0) == {}
        assert wes_draft_log.timings(None, wall=1.0) == {}

    def test_the_endpoint_serves_the_new_fields_uniformly(self):
        """Records written before this existed have no timing keys at all --
        the same null-vs-missing trap that killed the panel this morning."""
        import wes_server as ws
        wes_draft_log.log_call("draft.pick", {}, "ok", 1.0, model="m")   # no timings
        wes_draft_log.log_call("draft.pick", {}, "ok", 40.0, model="m",
                               load_s=0.0, prompt_s=1.0, gen_s=2.0,
                               queued_s=37.0, prompt_tok=600, gen_tok=80)
        with ws.app.test_client() as c:
            rows = c.get("/draft_turns").get_json()["turns"]
        for key in ("load_s", "prompt_s", "gen_s", "queued_s"):
            assert all(key in r for r in rows), f"{key} missing from a row"
        newest = rows[0]
        assert newest["queued_s"] == 37.0
        assert rows[1]["queued_s"] is None      # absent, honestly, not 0.0
