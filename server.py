#!/usr/bin/env python3
"""
AGENTSCII live dashboard server.
Forked from antfarm2-dashboard/server.py (SQLite live-view pattern, control
endpoints, stdlib-only HTTP server are proven and reused). New here: a
prompt-box endpoint that writes to human_messages (the inbox pattern,
delivered at shift start, never interrupting live inference); a Gallery
tab split into unpacked/ (accepted, pending release) and shipped packNN/
releases with real FILE_ID.DIZ credits; a Submissions/Rejected view with
contributor credits sidecars; and self-chosen agent handles surfaced next
to the functional artist/curator seat labels.
"""
import sqlite3
import json
import os
import signal
import subprocess
import threading
import time
import http.server
import socketserver
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOME = Path.home()
PROJECT_DIR = HOME / "agentscii"
WORKSPACE_DIR = PROJECT_DIR / "workspace"
GALLERY_DIR = WORKSPACE_DIR / "gallery"
GALLERY_UNPACKED_DIR = GALLERY_DIR / "unpacked"
SUBMISSIONS_DIR = WORKSPACE_DIR / "submissions"
REJECTED_DIR = WORKSPACE_DIR / "rejected"
SCRATCH_DIR = WORKSPACE_DIR / "scratch"
DB_PATH = PROJECT_DIR / "state.db"
STOP_FLAG = PROJECT_DIR / "STOP"
STATIC_DIR = Path(__file__).parent / "static"

AGENTS_MODEL = {
    "artist": "qwen3.8-27b-obliterated",
    "curator": "qwen3.8-27b-obliterated",
}

PORT = 8766  # antfarm2-dashboard already owns 8765


def get_db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get_db_rw():
    """Write connection, only for endpoints that actually insert (inbox posts)."""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_shifts(agent, limit=20):
    conn = get_db()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, agent, started_at, ended_at, note FROM shifts WHERE agent=? ORDER BY started_at DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_events(shift_id, limit=200):
    conn = get_db()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, agent, shift_id, role, content, reasoning, tool_name, tool_args, tool_call_id, timestamp "
            "FROM events WHERE shift_id=? ORDER BY id ASC LIMIT ?",
            (shift_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_status():
    conn = get_db()
    if not conn:
        return {"artist": {"active": False}, "curator": {"active": False}}
    try:
        out = {}
        for agent in ("artist", "curator"):
            row = conn.execute(
                "SELECT id, started_at, ended_at FROM shifts WHERE agent=? ORDER BY started_at DESC LIMIT 1",
                (agent,),
            ).fetchone()
            if row:
                out[agent] = {
                    "active": row["ended_at"] is None,
                    "shift_id": row["id"],
                    "started_at": row["started_at"],
                }
            else:
                out[agent] = {"active": False, "shift_id": None, "started_at": None}
        return out
    finally:
        conn.close()


def fetch_handles():
    conn = get_db()
    if not conn:
        return {}
    try:
        rows = conn.execute("SELECT seat, handle FROM agent_identity").fetchall()
        return {r["seat"]: r["handle"] for r in rows}
    finally:
        conn.close()


def fetch_stats(agent):
    conn = get_db()
    if not conn:
        return {}
    try:
        total_shifts = conn.execute("SELECT COUNT(*) c FROM shifts WHERE agent=?", (agent,)).fetchone()["c"]
        tool_rows = conn.execute(
            "SELECT tool_name FROM events WHERE agent=? AND role='assistant' AND tool_name IS NOT NULL",
            (agent,),
        ).fetchall()
        tool_counts = {}
        for r in tool_rows:
            n = r["tool_name"]
            tool_counts[n] = tool_counts.get(n, 0) + 1
        last = conn.execute("SELECT MAX(started_at) m FROM shifts WHERE agent=?", (agent,)).fetchone()["m"]
        return {"total_shifts": total_shifts, "tool_usage": tool_counts, "last_active": last}
    finally:
        conn.close()


def fetch_comms(limit=20):
    conn = get_db()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, from_agent, to_agent, text, timestamp FROM agent_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def fetch_curation_feed(limit=30):
    conn = get_db()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, shift_id, action, path, dest_path, note, timestamp FROM curation_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def fetch_human_inbox(limit=20):
    conn = get_db()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, to_agent, text, timestamp, delivered FROM human_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def post_human_message(to_agent, text):
    if to_agent not in ("artist", "curator", "both"):
        to_agent = "both"
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "empty message"}
    conn = get_db_rw()
    try:
        conn.execute(
            "INSERT INTO human_messages (to_agent, text, timestamp, delivered) VALUES (?,?,?,0)",
            (to_agent, text, time.time()),
        )
        conn.commit()
        return {"ok": True, "message": f"queued for {to_agent}, delivered at next shift start"}
    finally:
        conn.close()


def _list_dir_files(d, with_content=False, max_bytes=20000):
    if not d.exists():
        return []
    out = []
    for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        entry = {"path": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
        if with_content and f.stat().st_size < max_bytes:
            try:
                entry["content"] = f.read_text(errors="replace")
            except Exception:
                entry["content"] = None
        out.append(entry)
    return out


def fetch_gallery_unpacked():
    return _fetch_sidecar_dir(GALLERY_UNPACKED_DIR)


def fetch_rejected():
    return _fetch_sidecar_dir(REJECTED_DIR)


def _fetch_sidecar_dir(d, with_content=True):
    files = _list_dir_files(d, with_content=with_content)
    pieces = {}
    for f in files:
        name = f["path"]
        if name.endswith((".critique.txt", ".note.txt", ".credits.txt")) or name == "FILE_ID.DIZ":
            continue
        pieces[name] = {**f, "critique": None, "note": None, "credits": None}
    for f in files:
        name = f["path"]
        if name.endswith(".critique.txt"):
            base = name[: -len(".critique.txt")]
            if base in pieces:
                pieces[base]["critique"] = f.get("content")
        elif name.endswith(".note.txt"):
            base = name[: -len(".note.txt")]
            if base in pieces:
                pieces[base]["note"] = f.get("content")
        elif name.endswith(".credits.txt"):
            base = name[: -len(".credits.txt")]
            if base in pieces:
                pieces[base]["credits"] = f.get("content")
    return list(pieces.values())


def fetch_submissions():
    return _fetch_sidecar_dir(SUBMISSIONS_DIR)


def fetch_scratch():
    return _list_dir_files(SCRATCH_DIR, with_content=False)


def fetch_packs():
    """List shipped pack releases: gallery/packNN/ dirs, each with a
    FILE_ID.DIZ and its bundled pieces + sidecars."""
    if not GALLERY_DIR.exists():
        return []
    packs = []
    for d in sorted(GALLERY_DIR.iterdir(), reverse=True):
        if not d.is_dir() or not d.name.startswith("pack"):
            continue
        diz_path = d / "FILE_ID.DIZ"
        diz = diz_path.read_text(errors="replace") if diz_path.exists() else None
        pieces = _fetch_sidecar_dir(d)
        packs.append({
            "name": d.name,
            "mtime": d.stat().st_mtime,
            "file_id_diz": diz,
            "pieces": pieces,
        })
    return packs


# --- process control ---------------------------------------------------

def _pgrep_count(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5).stdout.strip()
        return len([l for l in out.split("\n") if l]) if out else 0
    except Exception:
        return 0


def control_status():
    return {
        "harness_running": _pgrep_count(r"agentscii/harness\.py") > 0,
        "watchdog_running": _pgrep_count(r"agentscii/watchdog\.sh") > 0,
        "stop_flag_present": STOP_FLAG.exists(),
    }


def control_start():
    status = control_status()
    STOP_FLAG.unlink(missing_ok=True)
    if status["watchdog_running"]:
        return {"ok": True, "message": "watchdog already running"}
    subprocess.Popen(
        ["nohup", "bash", "watchdog.sh"],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "message": "watchdog started"}


def control_stop():
    STOP_FLAG.touch()
    return {"ok": True, "message": "STOP flag set — harness will finish its current turn and shut down"}


def control_restart():
    STOP_FLAG.touch()

    def _wait_and_resume():
        for _ in range(90):
            time.sleep(1)
            if _pgrep_count(r"agentscii/harness\.py") == 0 and _pgrep_count(r"agentscii/watchdog\.sh") == 0:
                break
        STOP_FLAG.unlink(missing_ok=True)
        control_start()

    threading.Thread(target=_wait_and_resume, daemon=True).start()
    return {"ok": True, "message": "restarting: waiting for current turn to finish, then resuming"}


def restart_dashboard_server():
    def _do_restart():
        try:
            time.sleep(0.3)
            log_path = Path(__file__).resolve().parent / "restart.log"
            with open(log_path, "a") as logf:
                p = subprocess.Popen(
                    ["python3", str(Path(__file__).resolve())],
                    stdout=logf, stderr=logf,
                    start_new_session=True,
                )
            print(f"[restart] spawned replacement pid={p.pid}, log={log_path}")
            time.sleep(0.3)
            print(f"[restart] killing self pid={os.getpid()}")
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            print(f"[restart] FAILED: {e}")
    threading.Thread(target=_do_restart, daemon=True).start()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/sessions":
            agent = qs.get("agent", ["artist"])[0]
            shifts = fetch_shifts(agent)
            sessions = [{
                "id": s["id"], "source": "shift", "model": AGENTS_MODEL.get(agent, ""),
                "started_at": s["started_at"], "ended_at": s["ended_at"],
                "message_count": None, "note": s["note"],
            } for s in shifts]
            self._send_json({"sessions": sessions})
            return
        if parsed.path == "/api/messages":
            agent = qs.get("agent", ["artist"])[0]
            shift_id = qs.get("session_id", [None])[0]
            events = fetch_events(shift_id) if shift_id else []
            messages = []
            for e in events:
                if e["role"] == "system":
                    continue
                m = {
                    "id": e["id"], "session_id": e["shift_id"], "role": e["role"],
                    "content": e["content"], "tool_calls": None, "tool_name": e["tool_name"],
                    "timestamp": e["timestamp"], "reasoning": e.get("reasoning"),
                }
                if e["role"] == "assistant" and e["tool_name"]:
                    m["tool_calls"] = json.dumps([{
                        "function": {"name": e["tool_name"], "arguments": e["tool_args"]}
                    }])
                messages.append(m)
            self._send_json({"messages": messages})
            return

        if parsed.path == "/api/stats":
            self._send_json({"artist": fetch_stats("artist"), "curator": fetch_stats("curator")})
            return

        if parsed.path == "/api/status":
            st = fetch_status()
            handles = fetch_handles()
            self._send_json({
                "artist": {**st.get("artist", {}), "handle": handles.get("artist")},
                "curator": {**st.get("curator", {}), "handle": handles.get("curator")},
            })
            return

        if parsed.path == "/api/handles":
            self._send_json(fetch_handles())
            return

        if parsed.path == "/api/agent-messages":
            comms = fetch_comms()
            messages = [{
                "from_agent": c["from_agent"], "content": c["text"],
                "tool_calls": None, "timestamp": c["timestamp"],
            } for c in comms]
            self._send_json({"messages": messages})
            return

        if parsed.path == "/api/curation-feed":
            self._send_json({"events": fetch_curation_feed()})
            return

        if parsed.path == "/api/gallery":
            self._send_json({"pieces": fetch_gallery_unpacked()})
            return

        if parsed.path == "/api/packs":
            self._send_json({"packs": fetch_packs()})
            return

        if parsed.path == "/api/submissions":
            self._send_json({"pieces": fetch_submissions()})
            return

        if parsed.path == "/api/rejected":
            self._send_json({"pieces": fetch_rejected()})
            return

        if parsed.path == "/api/scratch":
            self._send_json({"files": fetch_scratch()})
            return

        if parsed.path == "/api/inbox":
            self._send_json({"messages": fetch_human_inbox()})
            return

        if parsed.path == "/api/control/status":
            self._send_json(control_status())
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/inbox":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
            result = post_human_message(data.get("to_agent", "both"), data.get("text", ""))
            self._send_json(result)
            return

        if parsed.path == "/api/control/start":
            self._send_json(control_start())
            return

        if parsed.path == "/api/control/stop":
            self._send_json(control_stop())
            return

        if parsed.path == "/api/control/restart":
            self._send_json(control_restart())
            return

        if parsed.path == "/api/control/restart-dashboard":
            self._send_json({"ok": True, "message": "dashboard restarting"})
            restart_dashboard_server()
            return

        self.send_response(404)
        self.end_headers()


def main():
    STATIC_DIR.mkdir(exist_ok=True)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    last_err = None
    for attempt in range(20):
        try:
            httpd = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError as e:
            last_err = e
            time.sleep(0.5)
    else:
        print(f"AGENTSCII dashboard: could not bind port {PORT} after retries: {last_err}")
        raise SystemExit(1)
    with httpd:
        print(f"AGENTSCII dashboard running at http://127.0.0.1:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
