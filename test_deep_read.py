#!/usr/bin/env python3
"""Self-check with an invented export and a pretend Ollama. No real data, no real model.
Run:  python test_deep_read.py   (prints OK if all is well)"""
import datetime as dt
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import deep_read as sc


def t(day, hour=12):
    return dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=dt.timezone.utc).timestamp()


def msg(mid, role, text, when, model=None, flag=False):
    md = {}
    if model:
        md["model_slug"] = model
    if flag:
        md["show_safety_switch_ui"] = True
    return {"id": mid, "author": {"role": role}, "create_time": when,
            "content": {"content_type": "text", "parts": [text]}, "metadata": md}


def conv(cid, turns, default="gpt-4o"):
    """turns: (user_text, reply_model, flagged, day[, reply_text[, regenerated_as]])
    regenerated_as: (model, flagged) for a second reply to the same message."""
    mapping, parent = {}, None
    for i, turn in enumerate(turns):
        u, model, flag, day = turn[:4]
        reply = turn[4] if len(turn) > 4 else "reply %d" % i
        uid, aid = "%s-u%d" % (cid, i), "%s-a%d" % (cid, i)
        mapping[uid] = {"message": msg(uid, "user", u, t(day, 10 + i)), "parent": parent, "children": [aid]}
        mapping[aid] = {"message": msg(aid, "assistant", reply, t(day, 10 + i) + 30, model, flag),
                        "parent": uid, "children": []}
        if len(turn) > 5:
            rid = aid + "-regen"
            m2, f2 = turn[5]
            mapping[rid] = {"message": msg(rid, "assistant", "regenerated", t(day, 10 + i) + 60, m2, f2),
                            "parent": uid, "children": []}
            mapping[uid]["children"].append(rid)
        if parent:
            mapping[parent]["children"].append(uid)
        parent = aid
    return {"id": cid, "default_model_slug": default, "mapping": mapping}


LONG = "x" * (sc.USER_LIMIT + 10)

CONVS = [
    conv("c1", [("hello", "gpt-4o", False, "2025-02-10", "hello darling"),
                ("how are you", "gpt-4o", False, "2025-02-11", "very well, sweetheart"),
                (LONG, "gpt-4o", False, "2025-02-12"),
                ("STRINGY answer please", "gpt-4o", False, "2025-02-13")]),
    conv("c2", [("rolled back days", "gpt-4o", False, "2025-04-26"),
                ("rolled back again", "gpt-4o", False, "2025-04-27")]),
    conv("c3", [("good morning", "gpt-4o", False, "2025-10-01"),
                ("I miss you", "gpt-5", True, "2025-10-01"),
                ("regen me", "gpt-5", True, "2025-10-01", "switched", ("gpt-4o", False)),
                ("the weather", "gpt-4o", False, "2025-10-02")]),
    conv("c4", [("a recipe please", "gpt-4o", False, "2025-10-01")]),
]


class FakeOllama(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"models": [{"name": "fake:1b"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = req["messages"][1]["content"]
        if "ASSISTANT REPLY" in text:
            ans = {k: ("rolled back" in text) for k in sc.MARKS}
            if "STRINGY" in text:
                ans = {k: "false" for k in sc.MARKS}      # the shape that used to count as yes
        else:
            ans = {"topic": "about_the_ai_or_relationship" if "miss" in text else "practical_or_other"}
            ans.update({k: ("miss" in text and k == "expresses_love_or_attachment_to_ai") for k in sc.FLAGS})
        self._send({"message": {"content": "```json\n" + json.dumps(ans) + "\n```"}})


def run(job, path, url, out, *extra):
    r = subprocess.run([sys.executable, os.path.join(HERE, "deep_read.py"), job, path,
                        "--model", "fake:1b", "--ollama", url, "--out", out] + list(extra),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return open(os.path.join(out, job + "-report.txt"), encoding="utf-8").read()


def row(report, name):
    return next(l for l in report.splitlines() if l.startswith(name))


def main():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "conversations.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(CONVS, f)
        convs = sc.load_conversations(path)
        assert len(convs) == 4

        # sampling is repeatable
        pool, totals = sc.collect_replies(convs, sc.DEFAULT_WINDOWS)
        assert totals["feb_2025"] == 4 and totals["rolled_back_version"] == 2, totals
        order = [w for w, _, _ in sc.DEFAULT_WINDOWS]
        assert sc.draw(pool, order, 1, 7) == sc.draw(pool, order, 1, 7)

        # strict answers: text "false" and missing keys are errors, never yes/no
        assert sc.strict_answers({"a": True, "b": False}, ["a", "b"]) == {"a": True, "b": False}
        for bad in ({"a": "false", "b": False}, {"a": True}, {"a": 1, "b": 0}, ["a"]):
            try:
                sc.strict_answers(bad, ["a", "b"])
                raise AssertionError("accepted %r" % (bad,))
            except ValueError:
                pass

        # triggers: one group per message, judged on all its replies
        groups, lo, hi = sc.collect_triggers(convs, None, None)
        switched = {r["user"] for r in groups["switched"]}
        compare = {r["user"] for r in groups["elsewhere"]} | {r["user"] for r in groups["same_conv"]}
        assert switched == {"I miss you", "regen me"}, switched
        assert "regen me" not in compare, "a message with a switched reply must not be a comparison"
        assert {r["user"] for r in groups["same_conv"]} == {"good morning"}
        assert {r["user"] for r in groups["elsewhere"]} == {"good morning", "a recipe please"}
        assert "the weather" not in compare        # the next day is outside the switch dates

        # duplicates are removed before sampling, so asking for n gives n
        dup = {"switched": [], "same_conv": [],
               "elsewhere": [{"id": "m%d" % (i % 9), "user": "u"} for i in range(12)]}
        assert len(sc.draw_triggers(dup, 8, 1)["elsewhere"]) == 8
        assert len(sc.draw_triggers(dup, 20, 1)["elsewhere"]) == 9

        # anything but this computer earns a warning
        assert sc.Reader("http://127.0.0.1:11434", "m").is_local()
        assert sc.Reader("http://localhost:11434", "m").is_local()
        assert not sc.Reader("http://example.org:11434", "m").is_local()

        srv = http.server.HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d" % srv.server_port
        out = os.path.join(tmp, "res")

        rep = run("replies", path, url, out, "--endearments", "darling")
        assert "100%" in row(rep, "rolled_back_version"), rep
        feb = row(rep, "  feb_2025")
        assert re.search(r"read\s+3\s+not read\s+1\s+cut short\s+1", feb), feb   # STRINGY not read; LONG cut
        assert re.search(r"\s33%\s", row(rep, "feb_2025")), row(rep, "feb_2025")  # darling: 1 of 3 read

        # changing the endearment words changes the report, without re-reading anything
        rep2 = run("replies", path, url, out, "--endearments", "darling,sweetheart")
        assert re.search(r"\s67%\s", row(rep2, "feb_2025")), row(rep2, "feb_2025")

        trig = run("triggers", path, url, out)
        assert "about_the_ai_or_relationship" in trig, trig
        for name in ("replies-report.txt", "triggers-report.txt", "replies-report.csv", "triggers-report.csv"):
            body = open(os.path.join(out, name), encoding="utf-8").read()
            for secret in ("I miss you", "good morning", "rolled back days", "darling", "regen me"):
                assert secret not in body, "message text leaked into %s" % name
        srv.shutdown()
        print("OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
