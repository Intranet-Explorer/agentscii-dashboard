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
        for name in ("_yes", "_no"):
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
                 "BASELINE_MD", "DESIGN_TXT")
        saved = {n: getattr(rs, n) for n in names}
        saved_db = harness.DB_PATH
        saved_hosts = (server.ALLOWED_HOSTS, server.ALLOWED_ORIGINS)
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            rs.ROOT, rs.PENDING, rs.UNPACKED, rs.OUT = d, pend, unp, out
            rs.REVIEWS_MD = os.path.join(d, "workspace", "REVIEWS.md")
            rs.PRIVATE, rs.BASELINE_MD, rs.DESIGN_TXT = priv, os.path.join(priv, "b.md"), design
            harness.DB_PATH = harness.Path(db)
            # Same rule as server.py, rebuilt for the test port.
            server.ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
            server.ALLOWED_ORIGINS = {f"http://{h}" for h in server.ALLOWED_HOSTS}

            ans = [{"file": "_yes.ans", "reads": True, "good": True, "publish": True, "note": "clean"},
                   {"file": "_no.ans", "reads": True, "good": False, "publish": False,
                    "note": "reads, shading is mush"}]

            # --- refusals: nothing may move or be written --------------
            for label, kw in [("no X-Agentscii", dict(header=False)),
                              ("foreign Host", dict(host=f"evil.example:{port}")),
                              ("foreign Origin", dict(origin="http://evil.example"))]:
                code, body = _req(port, "/api/review/apply", ans, **kw)
                assert code == 403, f"{label}: got {code} {body[:100]}"
            code, _ = _req(port, "/review", host=f"evil.example:{port}")
            assert code == 403, f"GET /review foreign Host: {code}"
            assert sorted(os.listdir(pend)) == ["_no.ans", "_no.ans.critique.txt",
                                                "_yes.ans", "_yes.ans.critique.txt"]
            assert not os.listdir(out), "a refused POST saved answers"
            print("  ok  POST without header / foreign Host / foreign Origin -> 403, nothing moved")

            # --- page ----------------------------------------------------
            code, page = _req(port, "/review")
            assert code == 200 and "_yes.ans" in page and "Save &amp; apply" in page, page[:200]
            assert "/api/review/apply" in page and "X-Agentscii" in page
            print("  ok  GET /review serves the sheet with Save & apply")

            # --- traversal is refused before anything moves --------------
            code, body = _req(port, "/api/review/apply",
                              [{"file": "../../etc/x.ans", "publish": True}])
            assert code == 200 and json.loads(body)["ok"] is False, body
            assert os.path.exists(os.path.join(pend, "_yes.ans"))
            assert not os.listdir(out), "refused answers were saved"
            print("  ok  answers naming a path outside pending/ are refused, nothing saved")

            # --- apply ---------------------------------------------------
            code, body = _req(port, "/api/review/apply", ans)
            r = json.loads(body)
            assert code == 200 and r["ok"], body
            assert os.path.exists(os.path.join(unp, "_yes.ans"))
            assert os.path.exists(os.path.join(unp, "_yes.ans.critique.txt")), "sidecar left behind"
            assert os.path.exists(os.path.join(pend, "_no.ans")), "unapproved piece moved"
            assert not os.path.exists(os.path.join(unp, "_no.ans"))
            saved_files = sorted(f for f in os.listdir(out) if f.endswith(".json"))
            assert len(saved_files) == 1 and json.load(open(os.path.join(out, saved_files[0]))) == ans
            c = sqlite3.connect(db)
            rows = c.execute("SELECT to_agent, text, timestamp FROM human_messages").fetchall()
            ev = c.execute("SELECT action, path FROM curation_events").fetchall()
            c.close()
            assert {x[0] for x in rows} == {"artist", "curator"}, rows
            assert all("reads, shading is mush" in x[1] for x in rows), "note not verbatim"
            assert ev == [("publish_approved", "_yes.ans")], ev
            assert "reads, shading is mush" in open(rs.REVIEWS_MD).read()
            text = open(design).read()
            assert f"FIRST DELIVERY: {rows[0][2]:.6f}" in text, text
            print(f"  ok  apply: 1 published + sidecar, 1 held, 2 seats messaged, "
                  f"answers in {saved_files[0]}, first delivery {rows[0][2]:.6f} recorded")

            # --- second apply: answers not overwritten, timestamp kept ---
            code, body = _req(port, "/api/review/apply",
                              [{"file": "_no.ans", "reads": False, "good": False,
                                "publish": False, "note": "second"}])
            assert json.loads(body)["ok"], body
            assert open(design).read() == text, "first delivery timestamp was overwritten"
            assert len([f for f in os.listdir(out) if f.endswith(".json")]) == 2
            print("  ok  second apply keeps the first timestamp and a separate answers file")

            # --- CLI path still works on the same functions --------------
            rs.build()
            assert any(f.endswith(".html") for f in os.listdir(out)), "CLI build wrote no sheet"
            print("  ok  CLI build still writes the sheet")
        finally:
            httpd.shutdown(); httpd.server_close()
            for n, v in saved.items():
                setattr(rs, n, v)
            harness.DB_PATH = saved_db
            server.ALLOWED_HOSTS, server.ALLOWED_ORIGINS = saved_hosts
    after = _live_snapshot()
    assert after == before, f"LIVE STATE CHANGED: {before} -> {after}"
    print("  ok  live state.db, EXPERIMENT_DESIGN.txt and pending/ untouched")
    print("  all checks passed")


if __name__ == "__main__":
    main()
