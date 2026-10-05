#!/usr/bin/env python3
"""End-to-end check of /review and /api/review/apply against a temp DB,
temp pending/, temp reviews/ and a temp private dir. Never touches the live
ones; asserts that at the end.

  python3 test_review_route.py
"""
import hashlib
import json
import os
import socketserver
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser("~/agentscii"))
import server  # noqa: E402
import review_sheet as rs  # noqa: E402
import harness  # noqa: E402

LIVE_DB = os.path.expanduser("~/agentscii/state.db")
LIVE_DESIGN = os.path.expanduser("~/agentscii-private/EXPERIMENT_DESIGN.txt")
LIVE_PENDING = os.path.expanduser("~/agentscii/workspace/pending")
PIECE = "\x1b[0;1;33m TEST \x1b[0;44m\u2588\u2588\u2580\u2580\u2584\u2584\x1b[0m\r\n" * 6


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _live_snapshot():
    c = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    try:
        hm = c.execute("SELECT MAX(id) FROM human_messages").fetchone()[0]
        ce = c.execute("SELECT MAX(id) FROM curation_events").fetchone()[0]
    finally:
        c.close()
    return hm, ce, _sha(LIVE_DESIGN), sorted(os.listdir(LIVE_PENDING))


def _req(port, path, body=None, host=None, header=True, origin=None):
    h = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json"}
    if header:
        h["X-Agentscii"] = "1"
    if origin:
        h["Origin"] = origin
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    before = _live_snapshot()
    with tempfile.TemporaryDirectory() as d:
        pend, unp = os.path.join(d, "pending"), os.path.join(d, "unpacked")
        out, priv = os.path.join(d, "reviews"), os.path.join(d, "private")
        for x in (pend, unp, out, priv, os.path.join(d, "workspace")):
            os.makedirs(x)
        names_all = ["_yes", "_no", "_skip"] + [f"_b{i}" for i in range(6)]
        for name in names_all:
            open(os.path.join(pend, f"{name}.ans"), "w").write(PIECE)
            open(os.path.join(pend, f"{name}.ans.critique.txt"), "w").write("crit")
        db = os.path.join(d, "state.db")
        c = sqlite3.connect(db)
        c.executescript(
            "CREATE TABLE human_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, to_agent TEXT NOT NULL,"
            " text TEXT NOT NULL, timestamp REAL NOT NULL, delivered INTEGER DEFAULT 0);"
            "CREATE TABLE curation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, shift_id INTEGER,"
            " action TEXT, path TEXT, dest_path TEXT, note TEXT, timestamp REAL);")
        c.close()
        design = os.path.join(priv, "EXPERIMENT_DESIGN.txt")
        open(design, "w").write("x\n    FIRST DELIVERY: <not yet>\ny\n")

        names = ("ROOT", "PENDING", "UNPACKED", "OUT", "REVIEWS_MD", "PRIVATE",
                 "BASELINE_MD", "DESIGN_TXT", "REVIEWED", "SETS_DIR")
        saved = {n: getattr(rs, n) for n in names}
        saved_db = harness.DB_PATH
        saved_hosts = (server.ALLOWED_HOSTS, server.ALLOWED_ORIGINS)
        saved_srv = (server.PENDING_DIR, server._notify_state, server._notify_macos)
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            rs.ROOT, rs.PENDING, rs.UNPACKED, rs.OUT = d, pend, unp, out
            rev = rs.REVIEWED = os.path.join(d, "reviewed")
            sets = rs.SETS_DIR = os.path.join(priv, "review_sets")
            server.PENDING_DIR = harness.Path(pend)
            server._notify_state = harness.Path(d) / ".pending_notified"
            notes = []
            server._notify_macos = lambda *a: notes.append(a)
            rs.REVIEWS_MD = os.path.join(d, "workspace", "REVIEWS.md")
            rs.PRIVATE, rs.BASELINE_MD, rs.DESIGN_TXT = priv, os.path.join(priv, "b.md"), design
            harness.DB_PATH = harness.Path(db)
            # Same rule as server.py, rebuilt for the test port.
            server.ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
            server.ALLOWED_ORIGINS = {f"http://{h}" for h in server.ALLOWED_HOSTS}

            no = {"file": "_no.ans", "reads": True, "good": False, "publish": False,
                  "note": "reads, shading is mush"}
            ans = ([{"file": "_yes.ans", "reads": True, "good": True, "publish": True, "note": "clean"}, no,
                    {"file": "_skip.ans", "reads": None, "good": None, "publish": None, "note": ""}]
                   + [{"file": f"_b{i}.ans", "reads": False, "good": False, "publish": False, "note": ""}
                      for i in range(6)])          # 8 answered + 1 skipped

            # --- refusals: nothing may move or be written --------------
            for label, kw in [("no X-Agentscii", dict(header=False)),
                              ("foreign Host", dict(host=f"evil.example:{port}")),
                              ("foreign Origin", dict(origin="http://evil.example"))]:
                code, body = _req(port, "/api/review/apply", ans, **kw)
                assert code == 403, f"{label}: got {code} {body[:100]}"
            code, _ = _req(port, "/review", host=f"evil.example:{port}")
            assert code == 403, f"GET /review foreign Host: {code}"
            assert len(os.listdir(pend)) == 18
            assert not os.listdir(out), "a refused POST saved answers"
            print("  ok  POST without header / foreign Host / foreign Origin -> 403, nothing moved")

            # --- page ----------------------------------------------------
            code, page = _req(port, "/review")
            assert code == 200 and "_yes.ans" in page and "Save &amp; apply" in page, page[:200]
            assert '{"pending": true, "min": 8}' in page, "baseline guard state not in page"
            assert "/api/review/apply" in page and "X-Agentscii" in page
            print("  ok  GET /review serves the sheet with Save & apply")

            # --- traversal is refused before anything moves --------------
            code, body = _req(port, "/api/review/apply",
                              [{"file": "../../etc/x.ans", "publish": True}])
            assert code == 200 and json.loads(body)["ok"] is False, body
            assert os.path.exists(os.path.join(pend, "_yes.ans"))
            assert not os.listdir(out), "refused answers were saved"
            print("  ok  answers naming a path outside pending/ are refused, nothing saved")

            # --- review sets: move nothing, hide provenance, deliver once ---
            os.makedirs(sets)
            gal = os.path.join(d, "gal"); os.makedirs(gal)
            for i in range(9):
                open(os.path.join(gal, f"_s{i}.ans"), "w").write(PIECE)
            snap = lambda: sorted((r, f) for r, _, fs in os.walk(d) for f in fs
                                  if not r.startswith(sets) and not r.startswith(out))
            json.dump({"mode": "benchmark", "deliver": False, "cards": [
                {"id": "x1", "path": os.path.join(gal, "_s0.ans"), "slug": "_s0", "model": "opus-5-5"},
                {"id": "x2", "path": os.path.join(gal, "_s1.ans"), "slug": "_s1", "model": "qwen"}]},
                open(os.path.join(sets, "bench.json"), "w"))
            json.dump({"mode": "backlog", "deliver": True, "cards": [
                {"id": f"b{i}", "path": os.path.join(gal, f"_s{i}.ans"), "slug": f"_s{i}"}
                for i in range(9)]}, open(os.path.join(sets, "backlog.json"), "w"))
            code, page = _req(port, "/review")
            assert 'href="/review?set=backlog"' in page and 'href="/review?set=bench"' in page
            code, page = _req(port, "/review?set=bench")
            assert code == 200 and "Save &amp; apply" in page, page[:300]
            for hidden in ("opus", "qwen", "model", gal, "_s0.ans"):
                assert hidden not in page, f"review page leaks {hidden!r}"
            assert '"/api/review/apply?set=bench"' in page
            before_files = snap()
            code, body = _req(port, "/api/review/apply?set=bench",
                              [{"id": "x1", "reads": True, "good": True, "publish": False, "note": "n1"},
                               {"id": "x2", "reads": False, "good": False, "publish": False, "note": ""}])
            assert json.loads(body)["ok"], body
            c = sqlite3.connect(db)
            assert c.execute("SELECT COUNT(*) FROM human_messages").fetchone()[0] == 0, \
                "a deliver:false set messaged the agents"
            c.close()
            assert "<not yet>" in open(design).read(), "deliver:false set recorded a first delivery"
            rec = json.load(open(os.path.join(sets, "bench.answers.json")))
            assert rec[0]["model"] == "opus-5-5" and rec[0]["note"] == "n1", rec
            code, body = _req(port, "/api/review/apply?set=bench", [{"id": "x1", "reads": True}])
            assert "already applied" in json.loads(body)["message"], body
            code, body = _req(port, "/api/review/apply?set=backlog", [{"id": "nope", "reads": True}])
            assert "not in set" in json.loads(body)["message"], body
            code, body = _req(port, "/api/review/apply?set=../x", [])
            assert not json.loads(body)["ok"], body
            code, body = _req(port, "/api/review/apply?set=backlog",
                              [{"id": f"b{i}", "reads": True, "good": False, "publish": False, "note": ""}
                               for i in range(3)])
            assert "BASELINE_TOO_SMALL" in json.loads(body)["message"], body
            code, body = _req(port, "/api/review/apply?set=backlog",
                              [{"id": f"b{i}", "reads": i % 2 == 0, "good": False, "publish": True,
                                "note": "backlog note" if i == 0 else ""} for i in range(9)])
            assert json.loads(body)["ok"], body
            moved = set(snap()) ^ set(before_files)
            # The delivery itself writes REVIEWS.md and EXPERIMENT_DESIGN.txt; nothing else.
            assert {f for _, f in moved} <= {"REVIEWS.md", "EXPERIMENT_DESIGN.txt"}, moved
            c = sqlite3.connect(db)
            rows = c.execute("SELECT to_agent, text, timestamp FROM human_messages").fetchall()
            c.execute("DELETE FROM human_messages"); c.commit(); c.close()
            assert {r[0] for r in rows} == {"artist", "curator"} and "backlog note" in rows[0][1]
            assert "_s8" in rows[0][1] and f"FIRST DELIVERY: {rows[0][2]:.6f}" in open(design).read()
            assert os.path.exists(os.path.join(unp)) and not os.listdir(unp), "publish=yes in a set published"
            print("  ok  sets: provenance hidden from the page, deliver:false sends nothing, "
                  "backlog set delivers 9 to both seats + records FIRST DELIVERY, moves nothing, "
                  "re-apply / foreign ids / bad name refused")
            open(design, "w").write("x\n    FIRST DELIVERY: <not yet>\ny\n")   # reset for the pending tests

            # --- small first delivery refused at the server, no override --
            code, body = _req(port, "/api/review/apply", ans[:2])
            r = json.loads(body)
            assert not r["ok"] and "BASELINE_TOO_SMALL" in r["message"] and "with 2 " in r["message"], body
            code, body = _req(port, "/api/review/apply", ans[:2] + [{"force": True}])
            assert not json.loads(body)["ok"], "dashboard accepted an override"
            assert len(os.listdir(pend)) == 18 and not os.listdir(out), "refused apply moved/saved"
            print("  ok  first delivery under 8 refused by the server (2 reviewed), nothing moved")

            assert server._pending_review_count() == 9
            # --- apply ---------------------------------------------------
            code, body = _req(port, "/api/review/apply", ans)
            r = json.loads(body)
            assert code == 200 and r["ok"], body
            assert os.path.exists(os.path.join(unp, "_yes.ans"))
            assert os.path.exists(os.path.join(unp, "_yes.ans.critique.txt")), "sidecar left behind"
            assert sorted(os.listdir(pend)) == ["_skip.ans", "_skip.ans.critique.txt"], os.listdir(pend)
            assert not os.path.exists(os.path.join(unp, "_no.ans"))
            assert os.path.exists(os.path.join(rev, "_no.ans.critique.txt")), "sidecar not in reviewed/"
            assert json.load(open(os.path.join(rev, "_no.ans.review.json"))) == no
            assert len([f for f in os.listdir(rev) if f.endswith(".ans")]) == 7
            assert server._pending_review_count() == 1, "pending count includes reviewed pieces"
            saved_files = sorted(f for f in os.listdir(out) if f.endswith(".json"))
            assert len(saved_files) == 1 and json.load(open(os.path.join(out, saved_files[0]))) == ans
            c = sqlite3.connect(db)
            rows = c.execute("SELECT to_agent, text, timestamp FROM human_messages").fetchall()
            ev = c.execute("SELECT action, path FROM curation_events").fetchall()
            c.close()
            assert {x[0] for x in rows} == {"artist", "curator"}, rows
            assert all("reads, shading is mush" in x[1] for x in rows), "note not verbatim"
            assert ev[0] == ("publish_approved", "_yes.ans") and len(ev) == 8 and \
                {e[0] for e in ev[1:]} == {"review_not_published"}, ev
            assert "reads, shading is mush" in open(rs.REVIEWS_MD).read()
            text = open(design).read()
            assert f"FIRST DELIVERY: {rows[0][2]:.6f}" in text, text
            print(f"  ok  apply: 1 -> unpacked, 7 -> reviewed/ with answers, 1 unanswered stays, "
                  f"pending count 9 -> 1, 2 seats messaged, "
                  f"answers in {saved_files[0]}, first delivery {rows[0][2]:.6f} recorded")

            # --- second apply: under 8 is fine now, timestamp kept --------
            code, page = _req(port, "/review")
            assert '{"pending": false, "min": 8}' in page
            code, body = _req(port, "/api/review/apply",
                              [{"file": "_skip.ans", "reads": False, "good": False,
                                "publish": False, "note": "second"}])
            assert json.loads(body)["ok"], body
            assert server._pending_review_count() == 0
            assert open(design).read() == text, "first delivery timestamp was overwritten"
            assert len([f for f in os.listdir(out) if f.endswith(".json")]) == 2
            print("  ok  later apply of 1 piece allowed, keeps the first timestamp, separate answers file")

            # --- CLI path still works on the same functions --------------
            open(os.path.join(pend, "_cli.ans"), "w").write(PIECE)
            rs.build()
            assert any(f.endswith(".html") for f in os.listdir(out)), "CLI build wrote no sheet"
            print("  ok  CLI build still writes the sheet")
        finally:
            httpd.shutdown(); httpd.server_close()
            for n, v in saved.items():
                setattr(rs, n, v)
            harness.DB_PATH = saved_db
            server.ALLOWED_HOSTS, server.ALLOWED_ORIGINS = saved_hosts
            server.PENDING_DIR, server._notify_state, server._notify_macos = saved_srv
    after = _live_snapshot()
    assert after == before, f"LIVE STATE CHANGED: {before} -> {after}"
    print("  ok  live state.db, EXPERIMENT_DESIGN.txt and pending/ untouched")
    print("  all checks passed")


if __name__ == "__main__":
    main()
