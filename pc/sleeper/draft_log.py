"""What the draft agents were actually asked, and what they actually said.

THE GAP THIS CLOSES. `wes_server` logs every voice and Discord exchange through
`record_turn`, and the Grafana "Recent turns" panel shows them verbatim. The
draft agents do not go through the server at all -- `wes_draft_agent` and
`wes_banter` both POST straight to Ollama -- so nothing they did reached
turns.jsonl or the dashboard. What survived a whole draft was the one-line
`reason` on each pick and a truncated `<- re:` fragment on each chat message.

That cost real time. Working out why a pick came out the way it did meant
re-running `draft_candidates` afterwards to reconstruct the board the model had
seen, and every suspect chat line needed an API round trip to check. The inputs
were never wrong -- they were simply never written down.

A SEPARATE FILE, DELIBERATELY. The obvious move is to append to turns.jsonl
with channel="draft" and get the existing panel for free. But that file is
written by the SERVER process, and its trim is read-all-then-rewrite: a draft
process appending during that window loses the append, and could lose server
records too. One writer per file. `wes_server.recent_turns` merges the two when
it serves them, which is a read, and reads do not race.

Records match the server's shape (ts/channel/transcript/reply/tools/error) so
the existing table renders them with no changes, plus the draft-specific parts
under keys the panel ignores.
"""
import json
import os
import threading
import time

LOG = os.environ.get(
    "WES_DRAFT_TURNS_LOG",
    os.path.join(os.path.expanduser("~"), "wes-pc", "logs",
                 "draft_turns.jsonl"))
MAX = int(os.environ.get("WES_DRAFT_TURNS_MAX", "2000"))
_TRIM_BYTES = 4_000_000

_lock = threading.Lock()


def log_call(kind, payload, reply, seconds=None, error=None, **extra):
    """One model call: what went in, what came back, how long it took.

    `kind` is the caller -- "draft.pick", "draft.explain", "draft.banter" --
    and lands in `channel` so one panel can show them and a filter can separate
    them.

    THE PAYLOAD GOES IN WHOLE. Summarising it here would recreate exactly the
    problem this exists to solve: the shortlist the model saw, in the order it
    saw it, IS the question when a pick looks wrong. It is a few KB and the
    file trims itself.

    Never raises. A logger that can break a draft is worse than no logger.
    """
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": kind,
        # The server's panel reads `transcript`/`reply`; keeping those names
        # means the existing table shows draft calls with no changes to it.
        "transcript": _stringify(payload),
        "reply": _stringify(reply),
        "tools": [],
        "escalated": False,
        "seconds": round(seconds, 2) if seconds is not None else None,
    }
    rec.update(extra)
    if error or not (rec["reply"] or "").strip():
        rec["error"] = error or "empty_reply"
    try:
        with _lock:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            if os.path.getsize(LOG) > _TRIM_BYTES:
                with open(LOG, encoding="utf-8") as f:
                    tail = f.readlines()[-MAX:]
                with open(LOG, "w", encoding="utf-8") as f:
                    f.writelines(tail)
    except OSError as e:
        print(f"[draft-log] write failed: {e}", flush=True)
    except Exception as e:  # noqa: BLE001 — see the docstring
        print(f"[draft-log] {type(e).__name__}: {e}", flush=True)


def _stringify(v):
    if v is None or isinstance(v, str):
        return v or ""
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return repr(v)


def recent(n=20, kind=None):
    """The last n calls, newest first. For tests and for the server merge."""
    try:
        with _lock, open(LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if kind and rec.get("channel") != kind:
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return out
