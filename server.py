#!/usr/bin/env python3
"""
AGENTSCII live dashboard server.
Forked from antfarm2-dashboard/server.py (SQLite live-view pattern, control
endpoints, stdlib-only HTTP server are proven and reused). New here: a
prompt-box endpoint that writes to human_messages (the inbox pattern,
delivered at shift start, never interrupting live inference); a Gallery
tab split into unpacked/ (accepted, pending release) and shipped packNN/
releases with real FILE_ID.DIZ credits; a Submissions/Rejected view with
contributor credits sidecars; self-chosen agent handles surfaced next
to the functional artist/curator seat labels; a real ANSI-to-HTML
renderer (SGR color codes -> styled spans, UTF-8/CP437-aware decoding)
so pieces actually render as art instead of raw escaped text; and a
live Scratch/WIP tab into workspace/scratch/ so in-progress pieces are
visible while the agents are still working on them.
"""
import sqlite3
import json
import os
import re
import html as html_mod
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
    "artist": "qwen3.8:27b-mlx",
    "curator": "qwen3.8:27b-mlx",
}

PORT = 8766  # antfarm2-dashboard already owns 8765

ANSI_EXTS = (".ans", ".asc")

# Classic 16-color DOS/CGA-style palette, matching the convention every
# generator script in this project already uses (c(fg,bg): fg/bg 0-7 normal,
# 8-15 bright via the 90-97/100-107 SGR range) — not the xterm defaults,
# which read too muted for BBS-style block art.
PALETTE = [
    "#000000", "#aa0000", "#00aa00", "#aa5500",
    "#0000aa", "#aa00aa", "#00aaaa", "#aaaaaa",
    "#555555", "#ff5555", "#55ff55", "#ffff55",
    "#5555ff", "#ff55ff", "#55ffff", "#ffffff",
]

_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_CSI_RE = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")
_TERMINAL_WIDTH = 80


def decode_ans_bytes(raw):
    """.ans/.asc files in this project are UTF-8 (every generator script
    writes real Unicode block chars), but real period pieces fetched from
    16colo.rs are genuine CP437 — support both rather than assuming."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp437", errors="replace")


def ansi_to_html(text):
    """Convert SGR-coded ANSI text into HTML: styled <span> runs, real
    colors from PALETTE. Returns the inner HTML only — caller wraps it in
    a <pre> with the right font/line-height.

    Real cursor-addressable grid model, not a flat left-to-right text scan.
    Classic ACiD/Blocktronics-scene .ANS files (and this project's own
    references/study/ corpus) routinely draw a base layer, then jump the
    cursor BACK UP with ESC[A to layer highlights/shadows onto rows already
    drawn. The old version here only understood SGR color codes and passed
    every other escape sequence through unconsumed — cursor-repositioned
    content didn't overwrite anything, it just got appended after in
    linear order, visibly duplicating/misplacing content. This mirrors the
    fix already made to harness.py's render_ans_to_png_b64 so both
    renderers treat the same file the same way."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    grid = {}
    row, col = 0, 0
    max_row_seen = 0
    base_fg, bright_fg, base_bg = 7, False, 0
    pos = 0
    n = len(text)
    pending_wrap = False  # deferred wrap, like a real terminal: filling the
                          # last column doesn't advance the row until the
                          # NEXT char is drawn -- avoids double-advancing on
                          # an explicit \n right after a full-width line.

    def put(ch):
        nonlocal col, row, max_row_seen, pending_wrap
        if pending_wrap:
            row += 1
            col = 0
            pending_wrap = False
            if row > max_row_seen:
                max_row_seen = row
        fg_idx = (base_fg + 8) if bright_fg else base_fg
        grid[(row, col)] = (ch, fg_idx % 16, base_bg % 16)
        col += 1
        if col >= _TERMINAL_WIDTH:
            col = _TERMINAL_WIDTH - 1
            pending_wrap = True

    while pos < n:
        ch = text[pos]
        if ch == "\n":
            if pending_wrap:
                pending_wrap = False
            else:
                row += 1
                col = 0
                if row > max_row_seen:
                    max_row_seen = row
            pos += 1
            continue
        m = _CSI_RE.match(text, pos)
        if m:
            param_str, code = m.group(1), m.group(2)
            params = [int(c) for c in param_str.split(";") if c != ""]
            if code == "m":
                for p in (params or [0]):
                    if p == 0:
                        base_fg, bright_fg, base_bg = 7, False, 0
                    elif p == 1:
                        bright_fg = True
                    elif p == 22:
                        bright_fg = False
                    elif p == 39:
                        base_fg, bright_fg = 7, False
                    elif p == 49:
                        base_bg = 0
                    elif 30 <= p <= 37:
                        base_fg = p - 30
                    elif 90 <= p <= 97:
                        base_fg, bright_fg = p - 90, True
                    elif 40 <= p <= 47:
                        base_bg = p - 40
                    elif 100 <= p <= 107:
                        base_bg = p - 100 + 8
            elif code == "C":
                col = min(_TERMINAL_WIDTH - 1, col + (params[0] if params else 1))
                pending_wrap = False
            elif code == "D":
                col = max(0, col - (params[0] if params else 1))
                pending_wrap = False
            elif code == "A":
                row = max(0, row - (params[0] if params else 1))
                pending_wrap = False
            elif code == "B":
                row = row + (params[0] if params else 1)
                pending_wrap = False
                if row > max_row_seen:
                    max_row_seen = row
            elif code in ("H", "f"):
                r = params[0] - 1 if len(params) >= 1 and params[0] else 0
                c = params[1] - 1 if len(params) >= 2 and params[1] else 0
                row, col = max(0, r), max(0, min(_TERMINAL_WIDTH - 1, c))
                pending_wrap = False
                if row > max_row_seen:
                    max_row_seen = row
            # any other CSI final byte (K, J, etc.) is consumed and ignored.
            pos = m.end()
            continue
        put(ch)
        pos += 1

    total_rows = max_row_seen + 1
    out_lines = []
    for r in range(total_rows):
        parts = []
        last_fg, last_bg = None, None
        span_open = False
        # trim trailing default-styled blank cells so short rows don't pad
        # the HTML with meaningless empty spans
        last_col = -1
        for c in range(_TERMINAL_WIDTH):
            cell = grid.get((r, c))
            if cell is not None and cell != (" ", 7, 0):
                last_col = c
        for c in range(last_col + 1):
            cell = grid.get((r, c), (" ", 7, 0))
            ch, fg_idx, bg_idx = cell
            if (fg_idx, bg_idx) != (last_fg, last_bg):
                if span_open:
                    parts.append("</span>")
                parts.append(f'<span style="color:{PALETTE[fg_idx % 16]};background-color:{PALETTE[bg_idx % 16]}">')
                span_open = True
                last_fg, last_bg = fg_idx, bg_idx
            parts.append(html_mod.escape(ch))
        if span_open:
            parts.append("</span>")
        out_lines.append("".join(parts))
    return "\n".join(out_lines)


def render_ans_file(path):
    try:
        raw = path.read_bytes()
        text = decode_ans_bytes(raw)
        return ansi_to_html(text)
    except Exception:
        return None



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


def _list_dir_files(d, with_content=False, max_bytes=200000):
    if not d.exists():
        return []
    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    out = []
    for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        entry = {"path": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
        if with_content and f.suffix.lower() in IMAGE_EXTS and f.stat().st_size < max_bytes:
            try:
                import base64
                entry["image_b64"] = base64.b64encode(f.read_bytes()).decode()
                entry["image_mime"] = "image/png" if f.suffix.lower() == ".png" else "image/jpeg" if f.suffix.lower() in (".jpg", ".jpeg") else "image/gif" if f.suffix.lower() == ".gif" else "image/webp"
            except Exception:
                entry["image_b64"] = None
        elif with_content and f.stat().st_size < max_bytes:
            try:
                entry["content"] = f.read_text(errors="replace")
            except Exception:
                entry["content"] = None
            if f.suffix.lower() in ANSI_EXTS:
                entry["rendered_html"] = render_ans_file(f)
        elif with_content:
            entry["content"] = None
            entry["too_large"] = True
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


def _classify_scratch_file(name):
    """Bucket a scratch/ filename so the dashboard can group by piece and
    rank real WIP art above debug noise, instead of a flat alphabetical/
    mtime dump of all ~450 files. Heuristic, not authoritative -- based on
    the naming conventions the agents actually use (checked against the
    real file list): .bak/.bak.* suffixes and pre_/pre-/prefix-style
    version tags for superseded revisions, .err/.out for command-output
    captures, .note/.critique/.credits/.scope/.DONE/.JOINT_SHIPPED for
    sidecar metadata, .ans/.asc for actual art, everything else (mostly
    generator .py scripts) as source."""
    lower = name.lower()
    if lower.endswith((".ans", ".asc")):
        return "art"
    if lower.endswith((".note.txt", ".critique.txt", ".credits.txt", ".scope.txt")) or \
       lower.endswith((".done.txt", ".joint_shipped.txt")):
        return "sidecar"
    if ".bak" in lower or ".pre" in lower or lower.endswith((".err", ".out")):
        return "debug"
    return "source"


def fetch_scratch():
    """Live view of scratch/ WIP, grouped by piece basename the same way
    Unpacked/Submissions/Rejected already group a piece with its sidecars --
    scratch never got that treatment before, so it rendered as a flat list
    of ~450 files (generator scripts, stale .bak revisions, command-output
    captures, and actual WIP art all equal weight, no way to tell which is
    which at a glance). Groups by stripping the LONGEST known suffix (so
    `_wharf_v6.ans.bak` groups under `_wharf_v6`, not a stray `_wharf_v6.ans`
    bucket) and ranks each group's primary preview: newest .ans/.asc first,
    falling back to newest of anything if a piece has no art yet."""
    files = _list_dir_files(SCRATCH_DIR, with_content=True)
    KNOWN_SUFFIXES = sorted([
        ".note.txt", ".critique.txt", ".credits.txt", ".scope.txt",
        ".done.txt", ".joint_shipped.txt", ".ans.bak", ".py.bak",
    ], key=len, reverse=True)

    def base_of(name):
        lower = name.lower()
        for suf in KNOWN_SUFFIXES:
            if lower.endswith(suf):
                return name[: -len(suf)]
        # generic .bak/.pre*/.err/.out and any other single extension:
        # strip exactly one suffix so `_wharf.py` and `_wharf.ans` group
        # together but a whole chain like `.ans.bak.py` isn't over-stripped
        stem = Path(name).stem
        return stem

    groups = {}
    for f in files:
        f = dict(f)
        f["kind"] = _classify_scratch_file(f["path"])
        b = base_of(f["path"])
        groups.setdefault(b, []).append(f)

    out = []
    for base, members in groups.items():
        members.sort(key=lambda f: f["mtime"], reverse=True)
        art = [f for f in members if f["kind"] == "art"]
        primary = art[0] if art else members[0]
        newest_mtime = max(f["mtime"] for f in members)
        out.append({
            "base": base,
            "primary": primary,
            "members": members,
            "has_art": bool(art),
            "n_art": len(art),
            "n_source": sum(1 for f in members if f["kind"] == "source"),
            "n_debug": sum(1 for f in members if f["kind"] == "debug"),
            "mtime": newest_mtime,
        })
    out.sort(key=lambda g: g["mtime"], reverse=True)
    return out


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
        ["nohup", "bash", str(PROJECT_DIR / "watchdog.sh")],
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
            self._send_json({"pieces": fetch_scratch()})
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
