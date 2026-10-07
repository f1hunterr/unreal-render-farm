"""Unreal Render Farm master: dashboard, node registry, job queue and scheduler."""
from flask import Flask, request, jsonify, Response, send_from_directory
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse
import base64
import hmac
import ipaddress
import logging
import re
import requests
import os
import sys
import json
import threading
import time
import uuid
import sqlite3
from contextlib import closing
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("farm.master")


def load_env_file(path=None):
    """Read URF_* settings from farm.env (KEY=VALUE lines). Real environment variables win."""
    path = path or os.environ.get("URF_CONFIG") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "farm.env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key.startswith("URF_"):
                os.environ.setdefault(key, value)


load_env_file()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

# ------------------------------
# CONFIGURATION (environment variables or farm.env)
# ------------------------------
FARM_TOKEN = os.environ.get("URF_FARM_TOKEN", "")          # shared with every agent
DASH_USER = os.environ.get("URF_DASH_USER", "admin")
DASH_PASSWORD = os.environ.get("URF_DASH_PASSWORD", "")     # dashboard login (HTTP Basic)
BIND_HOST = os.environ.get("URF_MASTER_BIND", "0.0.0.0")
PORT = int(os.environ.get("URF_MASTER_PORT", "5000"))
AGENT_PORT = int(os.environ.get("URF_AGENT_PORT", "5001"))
LOG_FILE = os.environ.get("URF_LOG_FILE", "")
POLL_INTERVAL = 2  # seconds

# A node that goes IDLE without reporting a result for its job (e.g. the agent restarted)
LOST_JOB_GRACE_SECONDS = 20
# A node unreachable this long has its job handed to another node (it may still finish; first result wins)
OFFLINE_REQUEUE_SECONDS = 600

MIN_SECRET_LENGTH = 16
MAX_RETRIES = 5
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
QUEUE_JOB_ID_RE = re.compile(r"^q(\d+)-(\d+)$")
PRIORITIES = {0: "Rush", 1: "Normal", 2: "Low"}
OFFLINE_STAGES = {"OFFLINE", "AUTH ERROR", "CONNECTING"}

CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' https://cdnjs.cloudflare.com; "
    "font-src https://cdnjs.cloudflare.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

HTTP = requests.Session()

# ------------------------------
# DATABASE & STORAGE SETUP
# ------------------------------
BASE_DIR = os.environ.get("URF_MASTER_DIR", r"C:\UnrealRenderFarm\master")
DB_PATH = os.path.join(BASE_DIR, "render_farm.db")
NODES_FILE = os.path.join(BASE_DIR, "nodes.json")

RENDER_NODES = {}
NODES_LOCK = threading.Lock()
STATUS_CACHE = {}
LAST_PLAN = {}  # node -> (agent job id, ranges) of the split plan last applied from its status
STATUS_LOCK = threading.Lock()
DB_LOCK = threading.Lock()          # serializes all database writes
SCHEDULER_LOCK = threading.Lock()   # only one scheduling pass at a time
WAKE_SCHEDULER = threading.Event()


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return closing(conn)


def json_body():
    """The request's JSON object ({} for a missing, broken or non-object body)"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text_field(data, key):
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with connect_db() as conn:
        # Readers (dashboard, poller) never block the writer, and the writer never blocks them
        conn.execute("PRAGMA journal_mode=WAL")
    with connect_db() as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      timestamp TEXT,
                      node_name TEXT,
                      project TEXT,
                      sequence TEXT,
                      status TEXT,
                      duration TEXT,
                      frames_rendered INTEGER DEFAULT 0)''')
        columns = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
        if "detail" not in columns:
            conn.execute("ALTER TABLE history ADD COLUMN detail TEXT DEFAULT ''")
        if "saved_to" not in columns:
            conn.execute("ALTER TABLE history ADD COLUMN saved_to TEXT DEFAULT ''")
        # Last agent result already logged per node, so restarts never double-log
        conn.execute('''CREATE TABLE IF NOT EXISTS result_cursors
                     (node_name TEXT PRIMARY KEY,
                      boot_id TEXT,
                      last_seq INTEGER)''')
        # One row per sequence to render. Idle nodes pull from here.
        conn.execute('''CREATE TABLE IF NOT EXISTS jobs
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      batch_id TEXT,
                      project TEXT,
                      map TEXT,
                      config TEXT,
                      sequence TEXT,
                      priority INTEGER DEFAULT 1,
                      status TEXT DEFAULT 'QUEUED',
                      attempts INTEGER DEFAULT 0,
                      max_attempts INTEGER DEFAULT 3,
                      allowed_nodes TEXT DEFAULT '[]',
                      tried_nodes TEXT DEFAULT '[]',
                      node_name TEXT DEFAULT '',
                      agent_job_id TEXT DEFAULT '',
                      cancel_requested INTEGER DEFAULT 0,
                      detail TEXT DEFAULT '',
                      created_at TEXT,
                      updated_at TEXT,
                      assigned_at REAL DEFAULT 0)''')
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, priority, id)")
        # Frame-range tasks: a shot split into chunks is several jobs sharing a shot_id
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for column, ddl in (("frame_start", "INTEGER"), ("frame_end", "INTEGER"), ("warmup", "INTEGER DEFAULT 0"),
                            ("shot_id", "TEXT DEFAULT ''"), ("chunk_index", "INTEGER DEFAULT 1"),
                            ("chunk_count", "INTEGER DEFAULT 1"), ("split_mode", "TEXT DEFAULT ''"),
                            ("network_retries", "INTEGER DEFAULT 0"), ("kind", "TEXT DEFAULT 'render'"),
                            ("frames_written", "INTEGER DEFAULT 0"), ("output_dir", "TEXT DEFAULT ''"),
                            ("avoid_nodes", "TEXT DEFAULT '[]'"), ("dispatches", "INTEGER DEFAULT 0"),
                            ("saved_to", "TEXT DEFAULT ''")):
            if column not in job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
                if column == "dispatches":  # ids already used by jobs from before this column existed
                    conn.execute("UPDATE jobs SET dispatches = attempts + network_retries")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_by_shot ON jobs (shot_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_by_update ON jobs (updated_at)")
        # Farm-wide settings changed on the Admin tab (e.g. the shared cache folder)
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


SETTING_DEFAULTS = {"shared_ddc": "", "output_root": "", "fast_mode": ""}
# A folder every render computer can write to: \\server\share\... or a drive letter mapped on all of them
OUTPUT_DIR_RE = re.compile(r'^(?:\\\\[A-Za-z0-9_.\-]+\\|[A-Za-z]:[\\/])[^"<>|?*\r\n]*$')


def clean_output_dir(value):
    text = clean_pasted(value) if isinstance(value, str) else ""
    return text.replace("/", "\\").rstrip("\\")


def output_dir_for(job):
    """Folder for a render's frames: its own 'Save frames to', else <farm output folder>\\<project>\\<shot>"""
    if job["output_dir"]:
        return job["output_dir"]
    root = get_setting("output_root")
    if not root:
        return ""
    project = os.path.splitext(os.path.basename(job["project"].replace("\\", "/")))[0]
    return f"{root}\\{project}\\{{sequence_name}}"


def clean_saved_to(value):
    """A folder an agent says the frames went to, safe to store and show (or '')"""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch >= " ")[:400].strip()


def saved_to_for(row):
    """Where a job's frames are (or will be) saved, for the dashboard: (folder, how sure).
    'saved' = reported after the render; 'live' = the rendering computer says so now;
    'planned' = the farm's output folder; ('', 'preset') = the preset's own folder, not known yet."""
    if row["saved_to"]:
        return row["saved_to"], "saved"
    if row["status"] == "ASSIGNED":
        node = cached_status(row["node_name"])
        live = clean_saved_to(node.get("output_folder"))
        if live and node.get("job_id") == row["agent_job_id"]:
            return live, "live"
    if (row["kind"] or "render") != "render":
        return "", ""
    planned = output_dir_for(row)
    if planned:
        sequence = str(row["sequence"] or "").rsplit("/", 1)[-1].split(".")[0]
        return planned.replace("{sequence_name}", sequence or "{sequence_name}"), "planned"
    return "", "preset"
UNC_PATH_RE = re.compile(r'^\\\\[A-Za-z0-9_.\-]+\\[^"<>|*?\r\n]+$')  # \\server\share\folder


def get_setting(key):
    with connect_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else SETTING_DEFAULTS.get(key, "")


def clean_cache_path(value):
    """A network folder for the shared cache, as typed or pasted (quotes and a trailing slash removed)"""
    text = clean_pasted(value) if isinstance(value, str) else ""
    text = text.replace("/", "\\").rstrip("\\")
    return text


def _insert_history(conn, node_name, project, sequence, status, duration="--", frames=0, detail="", saved_to=""):
    conn.execute(
        "INSERT INTO history (timestamp, node_name, project, sequence, status, duration, frames_rendered, detail, "
        "saved_to) VALUES (?,?,?,?,?,?,?,?,?)",
        (now_text(), node_name, project, sequence, status, duration, frames, detail, saved_to))


def log_history(node_name, project, sequence, status, duration="--", frames=0, detail=""):
    with DB_LOCK, connect_db() as conn, conn:
        _insert_history(conn, node_name, project, sequence, status, duration, frames, detail)


def load_nodes():
    if not os.path.exists(NODES_FILE):
        return {}
    try:
        with open(NODES_FILE, "r") as f:
            content = f.read().strip()
        nodes = json.loads(content) if content else {}
        if not isinstance(nodes, dict):
            raise ValueError("nodes file must contain a JSON object")
        return nodes
    except ValueError as e:
        # Keep the damaged file instead of letting the next save overwrite it
        backup = f"{NODES_FILE}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
        os.replace(NODES_FILE, backup)
        logger.warning("%s was unreadable (%s); moved to %s. Starting with no nodes.", NODES_FILE, e, backup)
        return {}


def save_nodes(nodes):
    tmp = NODES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(nodes, f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, NODES_FILE)


def init_storage(base_dir=None):
    """Create the data folder, database and node registry. Called at startup, not on import."""
    global BASE_DIR, DB_PATH, NODES_FILE
    if base_dir:
        BASE_DIR = base_dir
    DB_PATH = os.path.join(BASE_DIR, "render_farm.db")
    NODES_FILE = os.path.join(BASE_DIR, "nodes.json")
    os.makedirs(BASE_DIR, exist_ok=True)
    init_db()
    with NODES_LOCK:
        RENDER_NODES.clear()
        RENDER_NODES.update(load_nodes())
    with STATUS_LOCK:
        STATUS_CACHE.clear()


# ------------------------------
# VALIDATION
# ------------------------------
def validate_node(name, ip):
    if not isinstance(name, str) or not NODE_NAME_RE.match(name):
        return "Node name may only use letters, digits, '.', '-' and '_' (max 64)"
    try:
        addr = ipaddress.ip_address(ip.strip() if isinstance(ip, str) else "")
    except ValueError:
        return "IP address is not valid"
    if addr.version != 4:
        return "Use the computer's IPv4 address (e.g. 192.168.1.30)"
    if addr.is_unspecified or addr.is_multicast or addr.is_link_local or addr.is_reserved:
        return "IP address is not a usable host address"
    if not (addr.is_private or addr.is_loopback):
        return "Render nodes must be on a private (LAN) address"
    return None


FRAME_RANGE_RE = re.compile(r"^\s*(\d{1,8})\s*[-:]\s*(\d{1,8})\s*$")
MAX_CHUNKS_PER_SHOT = 1000
NETWORK_RETRIES = 3  # extra tries per piece after a network drop, not counted against the shot's retries
AUTO_MIN_CHUNK = int(os.environ.get("URF_AUTO_MIN_CHUNK", "50"))  # frames; smaller pieces waste start-up time
MAX_JOBS_PER_LAUNCH = 5000


def parse_frame_range(text):
    """'101-200' or '101:200' -> (101, 200), inclusive. Empty -> None (render the whole sequence)."""
    if text is None or not str(text).strip():
        return None
    match = FRAME_RANGE_RE.match(str(text))
    if not match:
        raise ValueError(f"frame range '{text}' must look like 0-1000")
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        raise ValueError(f"frame range '{text}' ends before it starts")
    return start, end


def even_pieces(start, end, pieces, min_chunk):
    """Inclusive range -> at most `pieces` contiguous ranges of near-equal size, none under min_chunk
    (a short range stays whole): 0-55 for 2 computers is one piece, not 0-49 + a 6-frame 50-55"""
    total = end - start + 1
    pieces = max(1, min(pieces, total // max(1, min_chunk) or 1))
    base, extra = divmod(total, pieces)
    ranges, at = [], start
    for i in range(pieces):
        size = base + (1 if i < extra else 0)
        ranges.append((at, at + size - 1))
        at += size
    return ranges


def split_frames(start, end, chunk_size):
    """Inclusive range -> list of inclusive (start, end) chunks of at most chunk_size frames"""
    if not chunk_size or chunk_size <= 0:
        return [(start, end)]
    return [(s, min(s + chunk_size - 1, end)) for s in range(start, end + 1, chunk_size)]


COPY_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_/.]+'(/[^']+)'$")
CONTENT_FILE_RE = re.compile(r"^.*?[\\/]Content[\\/](.+?)\.(?:uasset|umap)$", re.IGNORECASE)


def clean_pasted(value):
    """Undo what copy tools add: quotes from Windows 'Copy as path', and the
    Class'/Game/Path.Asset' wrapper from Unreal's 'Copy Reference'."""
    text = str(value or "").strip().strip('"').strip()
    match = COPY_REFERENCE_RE.match(text)
    return match.group(1) if match else text


def clean_asset(value):
    """An Unreal asset path from whatever an artist pasted:
    /All/Game/X (Content Browser path) -> /Game/X; a .uasset/.umap file under Content -> /Game/...;
    a trailing .uasset/.umap is dropped."""
    text = clean_pasted(value)
    match = CONTENT_FILE_RE.match(text)
    if match and not text.startswith("/"):
        return "/Game/" + match.group(1).replace("\\", "/")
    if text.lower().startswith("/all/"):
        text = text[4:]
    return re.sub(r"\.(uasset|umap)$", "", text, flags=re.IGNORECASE)


def clean_project(value):
    """A .uproject path from whatever an artist pasted (quotes, a doubled .uproject)"""
    text = clean_pasted(value)
    return re.sub(r"(\.uproject)+$", ".uproject", text, flags=re.IGNORECASE)


def task_label(sequence, frame_start, frame_end):
    return sequence if frame_start is None else f"{sequence} [{frame_start}-{frame_end}]"


# ------------------------------
# AGENT COMMUNICATION
# ------------------------------
def agent_url(info, path):
    return f"http://{info['ip']}:{AGENT_PORT}{path}"


def agent_headers():
    return {"X-Farm-Token": FARM_TOKEN}


def offline_status(name, stage="OFFLINE", error=""):
    return {
        "node": name, "scene": "", "stage": stage, "progress": 0,
        "current_frame": 0, "total_frames": 0, "eta": "", "fps": 0,
        "cpu_usage": 0, "gpu_usage": None, "ram_usage": 0, "vram_usage": None,
        "last_result": None, "error": error,
    }


def cached_status(name):
    with STATUS_LOCK:
        return STATUS_CACHE.get(name) or offline_status(name, "CONNECTING")


# ------------------------------
# RESULTS -> HISTORY + QUEUE
# ------------------------------
def select_new_results(cursor, boot_id, results):
    """Pick agent results not yet logged. cursor is (boot_id, last_seq) or None.

    An agent restart (new boot_id) restarts its numbering, so everything it reports is new.
    """
    last_seq = cursor[1] if cursor and cursor[0] == boot_id else 0
    # only well-formed results count; anything else is skipped (and logged by record_results' caller)
    numbered = [r for r in (results if isinstance(results, list) else [])
                if isinstance(r, dict) and isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool)]
    fresh = sorted((r for r in numbered if r["seq"] > last_seq), key=lambda r: r["seq"])
    new_last = fresh[-1]["seq"] if fresh else last_seq
    return fresh, (boot_id, new_last)


def get_cursor(node_name):
    with connect_db() as conn:
        row = conn.execute("SELECT boot_id, last_seq FROM result_cursors WHERE node_name=?",
                           (node_name,)).fetchone()
    return tuple(row) if row else None


def _requeue_or_fail(conn, job, detail):
    """A failed attempt: queue again if attempts remain, otherwise mark FAILED"""
    status = "QUEUED" if job["attempts"] < job["max_attempts"] and not job["cancel_requested"] else (
        "CANCELLED" if job["cancel_requested"] else "FAILED")
    conn.execute("UPDATE jobs SET status=?, node_name='', detail=?, updated_at=? WHERE id=?",
                 (status, detail[:1000], now_text(), job["id"]))
    return status


def _expand_auto_job(conn, agent_job_id, discovered, stop=None):
    """A shared shot's first computer reported the shot's frames and the planned pieces:
    give the first piece to that job and queue the others. Returns the number of pieces, or 0.
    stop: a list that gets (node, agent job id) of renders to stop because they planned other frames."""
    match = QUEUE_JOB_ID_RE.match(agent_job_id or "")
    if not match or not isinstance(discovered, dict):
        return 0
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (int(match.group(1)),)).fetchone()
    if job and job["split_mode"] == "auto-piece":
        return _apply_piece_plan(conn, job, discovered, agent_job_id, stop)
    if not job or job["split_mode"] != "auto":
        return 0  # not a shared shot, or already split
    if job["status"] != "ASSIGNED" or job["agent_job_id"] != agent_job_id:
        return 0  # an old or cancelled run reporting late: its plan must not create pieces
    try:
        ranges = [(int(a), int(b)) for a, b in discovered.get("ranges") or []]
        first, last = int(discovered["start"]), int(discovered["end"])
    except (TypeError, ValueError, KeyError):
        return 0
    contiguous = all(b >= a for a, b in ranges) and all(
        ranges[i + 1][0] == ranges[i][1] + 1 for i in range(len(ranges) - 1))
    if (not ranges or not contiguous or ranges[0][0] != first or ranges[-1][1] != last
            or len(ranges) > MAX_CHUNKS_PER_SHOT):
        logger.warning("Ignoring invalid split plan for queue #%s: %s", job["id"], discovered)
        return 0
    stamp = now_text()
    if len(ranges) == 1:
        note = discovered.get("note") or f"Frames {first}-{last}: short enough for one computer"
        conn.execute("UPDATE jobs SET split_mode='auto-split', detail=?, updated_at=? WHERE id=?",
                     (note[:1000], stamp, job["id"]))
        return 1
    count = len(ranges)
    conn.execute("UPDATE jobs SET split_mode='auto-split', frame_start=?, frame_end=?, chunk_index=1, "
                 "chunk_count=?, detail=?, updated_at=? WHERE id=?",
                 (ranges[0][0], ranges[0][1], count,
                  f"Shared automatically: frames {first}-{last} in {count} pieces", stamp, job["id"]))
    conn.executemany(
        "INSERT INTO jobs (batch_id, project, map, config, sequence, priority, max_attempts, allowed_nodes, "
        "created_at, updated_at, frame_start, frame_end, warmup, shot_id, chunk_index, chunk_count, split_mode, "
        "kind, output_dir) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'auto-split',?,?)",
        [(job["batch_id"], job["project"], job["map"], job["config"], job["sequence"], job["priority"],
          job["max_attempts"], job["allowed_nodes"], job["created_at"], stamp, a, b, job["warmup"], job["shot_id"],
          index, count, job["kind"] or "render", job["output_dir"] or "")
         for index, (a, b) in enumerate(ranges[1:], 2)])
    logger.info("Queue #%s shared automatically: frames %s-%s in %s pieces", job["id"], first, last, count)
    return count


def _valid_plan(discovered):
    try:
        ranges = [(int(a), int(b)) for a, b in discovered.get("ranges") or []]
        first, last = int(discovered["start"]), int(discovered["end"])
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    contiguous = all(b >= a for a, b in ranges) and all(
        ranges[i + 1][0] == ranges[i][1] + 1 for i in range(len(ranges) - 1))
    if not ranges or not contiguous or ranges[0][0] != first or ranges[-1][1] != last:
        return None
    return first, last, ranges


def _apply_piece_plan(conn, job, discovered, agent_job_id="", stop=None):
    """A piece of a shared shot reported the plan: write every piece's frames (so retries render the
    same frames anywhere) and mark pieces the shot turned out not to need as done.

    The first plan reported is the shot's plan. Every computer should make the same one, but an older
    agent or a computer seeing an out-of-date copy of the sequence can plan differently, which would
    leave frames missing or rendered twice. Such a piece is stopped and queued again with the shot's
    frames written out, so it renders exactly those."""
    plan = _valid_plan(discovered)
    if not plan:
        logger.warning("Ignoring invalid split plan for queue #%s: %s", job["id"], discovered)
        return 0
    first, last, ranges = plan
    stamp = now_text()
    index = job["chunk_index"]
    if job["frame_start"] is not None:
        mine = tuple(ranges[index - 1]) if index <= len(ranges) else None
        if mine != (job["frame_start"], job["frame_end"]):
            shown = f"{mine[0]}-{mine[1]}" if mine else "nothing"
            note = (f"{job['node_name'] or 'A computer'} planned frames {shown} for this piece, but the shot's "
                    f"plan says {job['frame_start']}-{job['frame_end']} (older agent or out-of-date sequence "
                    "on that computer?). Rendering the shot's frames again.")
            if job["status"] == "ASSIGNED" and job["agent_job_id"] == agent_job_id:
                # a new attempt number, so the stopped render's late report can never be taken for the new one
                conn.execute("UPDATE jobs SET status='QUEUED', node_name='', detail=?, updated_at=? WHERE id=?",
                             (note, stamp, job["id"]))
                if stop is not None:
                    stop.append((job["node_name"], agent_job_id))
                logger.warning("Queue #%s: %s", job["id"], note)
            return len(ranges)
    for piece in conn.execute("SELECT * FROM jobs WHERE shot_id=? AND split_mode='auto-piece'",
                              (job["shot_id"],)).fetchall():
        index = piece["chunk_index"]
        if index <= len(ranges):
            if piece["frame_start"] is None:
                a, b = ranges[index - 1]
                conn.execute("UPDATE jobs SET frame_start=?, frame_end=?, updated_at=? WHERE id=?",
                             (a, b, stamp, piece["id"]))
        elif piece["status"] == "QUEUED":
            conn.execute("UPDATE jobs SET status='SUCCESS', detail=?, updated_at=? WHERE id=?",
                         (f"Not needed: frames {first}-{last} fit in {len(ranges)} piece(s)", stamp, piece["id"]))
    return len(ranges)


def expand_auto_job(agent_job_id, discovered):
    stop = []
    with DB_LOCK, connect_db() as conn, conn:
        count = _expand_auto_job(conn, agent_job_id, discovered, stop)
    for node, stale_job_id in stop:
        if cached_status(node).get("job_id") == stale_job_id:  # still rendering the wrong frames
            _send_cancel(node)
    if count > 1 or stop:
        WAKE_SCHEDULER.set()
    return count


def _apply_result_to_job(conn, node_name, result):
    """Move the queue entry an agent result belongs to (agent job ids look like q<job>-<attempt>)"""
    match = QUEUE_JOB_ID_RE.match(result.get("job_id", ""))
    if not match:
        return
    if result.get("discovered"):
        _expand_auto_job(conn, result["job_id"], result["discovered"])
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (int(match.group(1)),)).fetchone()
    if not job:
        return
    outcome = result.get("status")
    current_attempt = job["status"] == "ASSIGNED" and job["agent_job_id"] == result["job_id"]
    folder = clean_saved_to(result.get("output_folder"))
    if folder and (current_attempt or outcome == "COMPLETED"):
        conn.execute("UPDATE jobs SET saved_to=? WHERE id=?", (folder, job["id"]))

    if outcome == "COMPLETED":
        reported = (result.get("frame_start"), result.get("frame_end"))
        if job["frame_start"] is not None and reported[0] is not None and \
                reported != (job["frame_start"], job["frame_end"]):
            # Rendered other frames than this piece needs (a plan that differed): not this piece's success
            logger.warning("Queue #%s: ignoring success for frames %s-%s; the piece is %s-%s",
                           job["id"], reported[0], reported[1], job["frame_start"], job["frame_end"])
            return
        if current_attempt or job["status"] in ("QUEUED", "FAILED"):
            # Also accepts a late success from a node we had given up on
            frames = result.get("frames") if isinstance(result.get("frames"), int) else 0
            conn.execute("UPDATE jobs SET status='SUCCESS', node_name=?, detail=?, frames_written=?, saved_to=?, "
                         "updated_at=? WHERE id=?",
                         (node_name, result.get("detail", "")[:1000], max(0, frames),
                          clean_saved_to(result.get("output_folder")), now_text(), job["id"]))
        return
    if not current_attempt:
        return  # stale report about an attempt we already moved on from
    if outcome == "CANCELLED" or job["cancel_requested"]:
        conn.execute("UPDATE jobs SET status='CANCELLED', detail='Cancelled', updated_at=? WHERE id=?",
                     (now_text(), job["id"]))
        return
    if result.get("network_error") and job["network_retries"] < NETWORK_RETRIES:
        # The project drive dropped while Unreal loaded: not the render's fault, so try again
        # straight away without using up one of the shot's retries
        tries = job["network_retries"] + 1
        conn.execute("UPDATE jobs SET status='QUEUED', attempts=MAX(0, attempts - 1), network_retries=?, "
                     "node_name='', detail=?, updated_at=? WHERE id=?",
                     (tries, f"Network drop on {node_name} while loading the project: retrying "
                             f"({tries} of {NETWORK_RETRIES}, not counted as a try)", now_text(), job["id"]))
        return
    if result.get("owner_returned"):
        # A workstation's owner came back: not the render's fault, run it elsewhere without using a try
        conn.execute("UPDATE jobs SET status='QUEUED', attempts=MAX(0, attempts - 1), node_name='', detail=?, "
                     "updated_at=? WHERE id=?",
                     (f"{node_name}: its owner started working, so the render stopped. Back in the queue "
                      "(not counted as a try).", now_text(), job["id"]))
        return
    if result.get("out_of_memory"):
        # The same computer would run out of memory again: try another one, or stop and say why
        avoid = sorted(set(json.loads(job["avoid_nodes"] or "[]")) | {node_name})
        with NODES_LOCK:
            registered = set(RENDER_NODES)
        allowed = [n for n in (json.loads(job["allowed_nodes"]) or sorted(registered)) if n in registered]
        conn.execute("UPDATE jobs SET avoid_nodes=? WHERE id=?", (json.dumps(avoid), job["id"]))
        if not [n for n in allowed if n not in avoid]:
            conn.execute("UPDATE jobs SET status='FAILED', node_name='', detail=?, updated_at=? WHERE id=?",
                         (f"{node_name} ran out of memory, so it was not tried again on the same computer. "
                          "Click Edit to add another computer, or lower the preset's settings (High Resolution "
                          f"tiles, number of outputs) or raise that PC's paging file. {result.get('detail', '')}"[:1000],
                          now_text(), job["id"]))
            return
        refreshed = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        _requeue_or_fail(conn, refreshed, f"Attempt {job['attempts']} ran out of memory on {node_name}; "
                                          f"trying another computer. {result.get('detail', '')}")
        return
    _requeue_or_fail(conn, job, f"Attempt {job['attempts']} failed on {node_name}: {result.get('detail', '')}")


def record_results(node_name, boot_id, results):
    """Log finished renders reported by an agent exactly once and update their queue entries"""
    with DB_LOCK:
        cursor = get_cursor(node_name)
        fresh, (cursor_boot, cursor_seq) = select_new_results(cursor, boot_id, results)
        if not fresh and cursor == (cursor_boot, cursor_seq):
            return 0
        with connect_db() as conn, conn:
            conn.execute("BEGIN")  # one transaction: savepoints below never commit on their own
            for r in fresh:
                conn.execute("SAVEPOINT one_result")
                try:
                    if not isinstance(r, dict):
                        raise ValueError(f"result is not an object: {r!r}"[:200])
                    status = "SUCCESS" if r.get("status") == "COMPLETED" else str(r.get("status", "UNKNOWN"))
                    _insert_history(conn, node_name, str(r.get("project") or ""),
                                    task_label(str(r.get("sequence") or ""), r.get("frame_start"), r.get("frame_end")),
                                    status, str(r.get("duration") or "--"), r.get("frames") or 0,
                                    str(r.get("detail") or ""),
                                    clean_saved_to(r.get("output_folder")))
                    _apply_result_to_job(conn, node_name, {**r, "detail": str(r.get("detail") or "")})
                    conn.execute("RELEASE one_result")
                except Exception:
                    conn.execute("ROLLBACK TO one_result")
                    conn.execute("RELEASE one_result")
                    logger.exception("Skipped a result from %s that could not be applied", node_name)
            conn.execute("INSERT OR REPLACE INTO result_cursors (node_name, boot_id, last_seq) VALUES (?,?,?)",
                         (node_name, cursor_boot, cursor_seq))
    if fresh:
        WAKE_SCHEDULER.set()
    return len(fresh)


# ------------------------------
# NODE POLLING (runs in the background, independent of any open dashboard)
# ------------------------------
def poll_node(name, info):
    try:
        r = HTTP.get(agent_url(info, "/status"), headers=agent_headers(), timeout=2)
        if r.status_code == 401:
            data = offline_status(name, "AUTH ERROR", "agent rejected the farm token")
        else:
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get("stage"), str):
                raise ValueError("not an agent status reply")
            data["node"] = name
            plan = data.get("discovered")
            if isinstance(plan, dict) and LAST_PLAN.get(name) != (plan.get("job_id"), str(plan.get("ranges"))):
                expand_auto_job(plan.get("job_id"), plan)  # the same plan is applied once, not every 2 s
                LAST_PLAN[name] = (plan.get("job_id"), str(plan.get("ranges")))
            boot_id = data.get("boot_id", "")
            cursor = get_cursor(name)
            known_seq = cursor[1] if cursor and cursor[0] == boot_id else 0
            if data.get("result_seq", 0) > known_seq:
                rr = HTTP.get(agent_url(info, "/results"), params={"since": known_seq},
                              headers=agent_headers(), timeout=2)
                rr.raise_for_status()
                payload = rr.json()
                record_results(name, payload.get("boot_id", boot_id), payload.get("results", []))
    except (requests.RequestException, ValueError) as e:
        data = offline_status(name, error=str(e)[:200])
    except Exception as e:  # never let one computer stop polling and scheduling for all of them
        logger.exception("Polling %s failed", name)
        data = offline_status(name, error=f"error reading its status: {e}"[:200])

    with NODES_LOCK:
        moved = (RENDER_NODES.get(name) or {}).get("ip") != info.get("ip")
    if moved:
        return data  # re-registered at another address while we polled the old one
    with STATUS_LOCK:
        previous = STATUS_CACHE.get(name) or {}
        if data["stage"] in OFFLINE_STAGES:
            data["offline_since"] = previous.get("offline_since") or time.time()
            # remember what the agent could do while it is unreachable
            for key in ("agent_version", "features"):
                if key in previous and key not in data:
                    data[key] = previous[key]
        STATUS_CACHE[name] = data
    return data


def poll_all_nodes():
    with NODES_LOCK:
        nodes = list(RENDER_NODES.items())
    if not nodes:
        return
    with ThreadPoolExecutor(max_workers=min(32, len(nodes))) as executor:
        list(executor.map(lambda x: poll_node(x[0], x[1]), nodes))


# ------------------------------
# SCHEDULER
# ------------------------------
def requeue_lost_jobs(now=None):
    """Give jobs back to the queue when their node lost them (restart) or has been unreachable for long"""
    now = now or time.time()
    with NODES_LOCK:
        registered = set(RENDER_NODES)
    with DB_LOCK, connect_db() as conn, conn:
        for job in conn.execute("SELECT * FROM jobs WHERE status='ASSIGNED'").fetchall():
            node = job["node_name"]
            status = cached_status(node)
            reason = None
            if node not in registered:
                reason = f"{node} was removed from the registry"
            elif status.get("stage") in ("IDLE", "IN USE") and now - job["assigned_at"] > LOST_JOB_GRACE_SECONDS:
                reason = f"{node} went idle without reporting a result (agent restarted?)"
            elif (status.get("stage") in OFFLINE_STAGES and status.get("offline_since")
                  and now - status["offline_since"] > OFFLINE_REQUEUE_SECONDS):
                reason = f"{node} unreachable for over {OFFLINE_REQUEUE_SECONDS // 60} minutes"
            if reason:
                new_status = _requeue_or_fail(conn, job, f"Attempt {job['attempts']} lost: {reason}")
                logger.warning("Queue #%s: %s -> %s", job["id"], reason, new_status)


OLD_AGENT_NOTE = ("Waiting: none of the allowed computers can render frame ranges yet. "
                  "Run the latest SETUP.bat on the render computers.")


NO_COMPUTER_NOTE = ("Waiting: none of the computers chosen for this render is registered any more. "
                    "Click Edit to choose others.")


def can_split(node, feature="frame_range"):
    """Does this node's agent (as last reported) support the feature?"""
    return feature in (cached_status(node).get("features") or [])


PREPARE_KINDS = ("prepare", "prepare-fill")


def needed_feature(job):
    if (job["kind"] or "render") in PREPARE_KINDS:
        return "prepare"
    if job["frame_start"] is not None:
        return "frame_range"
    if job["split_mode"] in ("auto", "auto-piece"):
        return "auto_piece"  # renders "piece i of N" of a shot whose frames it reads itself
    return None


def note_jobs_waiting_for_new_agents(queued, registered):
    """Tell people why a frame-range job is not starting when only old agents could take it"""
    stuck = []
    orphaned = [job["id"] for job in queued if json.loads(job["allowed_nodes"] or "[]")
                and not [n for n in json.loads(job["allowed_nodes"]) if n in registered]
                and job["detail"] != NO_COMPUTER_NOTE]
    if orphaned:
        with DB_LOCK, connect_db() as conn, conn:
            conn.executemany("UPDATE jobs SET detail=? WHERE id=? AND status='QUEUED'",
                             [(NO_COMPUTER_NOTE, job_id) for job_id in orphaned])
    for job in queued:
        feature = needed_feature(job)
        if not feature:
            continue
        allowed = [n for n in (json.loads(job["allowed_nodes"]) or sorted(registered)) if n in registered]
        if allowed and not any(can_split(n, feature) for n in allowed) and job["detail"] != OLD_AGENT_NOTE:
            stuck.append(job["id"])
    if stuck:
        with DB_LOCK, connect_db() as conn, conn:
            conn.executemany("UPDATE jobs SET detail=? WHERE id=? AND status='QUEUED'",
                             [(OLD_AGENT_NOTE, job_id) for job_id in stuck])


def pick_job(node, queued, registered):
    """Highest-priority job this node may run.

    A retry skips a node that already failed it while an untried node is idle right now, so the
    retry lands elsewhere; it never waits on busy or offline nodes.
    """
    for job in queued:
        allowed = json.loads(job["allowed_nodes"]) or sorted(registered)
        if node not in allowed or node in json.loads(job["avoid_nodes"] or "[]"):
            continue
        feature = needed_feature(job)
        if feature and not can_split(node, feature):
            continue  # an older agent would ignore the frame range / sharing and render the whole shot
        tried = set(json.loads(job["tried_nodes"]))
        if node not in tried:
            return job
        untried_idle = [n for n in allowed if n not in tried and n in registered
                        and cached_status(n).get("stage") == "IDLE"]
        if not untried_idle:
            return job
    return None


def share_candidates(allowed_json, feature="auto_piece"):
    """The selected computers that can take a piece: reachable now, with an agent that has `feature`.
    Busy ones count - they take their piece when they free up. Returns (usable, unable)."""
    with NODES_LOCK:
        registered = set(RENDER_NODES)
    allowed = [n for n in (json.loads(allowed_json or "[]") or sorted(registered)) if n in registered]
    usable, unable = [], []
    for n in allowed:
        if cached_status(n).get("stage") in OFFLINE_STAGES:
            unable.append((n, "offline"))
        elif cached_status(n).get("stage") == "IN USE":
            unable.append((n, "in use by its owner"))  # a workstation: don't plan a piece that would wait
        elif not can_split(n, feature):
            unable.append((n, "needs the latest SETUP.bat"))
        else:
            usable.append(n)
    return usable, unable


def auto_pieces(job, feature="auto_piece"):
    """How many pieces to cut a shared shot into: one per selected computer that can take one"""
    usable, _ = share_candidates(job["allowed_nodes"], feature)
    return max(1, min(64, len(usable)))


def split_into_pieces(job, pieces):
    """Turn a shared shot into `pieces` jobs (piece i of N) so every free computer starts at once.
    Each computer reads the shot's frames itself and renders its own piece of the same plan.
    Returns the (updated) row of piece 1."""
    if pieces <= 1:
        return job
    stamp = now_text()
    with DB_LOCK, connect_db() as conn, conn:
        updated = conn.execute(
            "UPDATE jobs SET split_mode='auto-piece', chunk_index=1, chunk_count=?, updated_at=? "
            "WHERE id=? AND split_mode='auto' AND status='QUEUED'", (pieces, stamp, job["id"])).rowcount
        if not updated:
            return job
        conn.executemany(
            "INSERT INTO jobs (batch_id, project, map, config, sequence, priority, max_attempts, allowed_nodes, "
            "created_at, updated_at, warmup, shot_id, chunk_index, chunk_count, split_mode, kind, output_dir) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'auto-piece',?,?)",
            [(job["batch_id"], job["project"], job["map"], job["config"], job["sequence"], job["priority"],
              job["max_attempts"], job["allowed_nodes"], job["created_at"], stamp, job["warmup"], job["shot_id"],
              index, pieces, job["kind"] or "render", job["output_dir"] or "") for index in range(2, pieces + 1)])
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    logger.info("Queue #%s shared between %s computers", job["id"], pieces)
    WAKE_SCHEDULER.set()  # hand out the other pieces right away
    return row


def dispatch_job(job, node, info):
    """Send one queued sequence to a node. Returns True if the node accepted it."""
    attempt = job["attempts"] + 1
    # every send gets its own id (also network retries and manual retries), so a late report from an
    # earlier run can never be taken for this one
    dispatch_no = (job["dispatches"] or 0) + 1
    agent_job_id = f"q{job['id']}-{dispatch_no}"
    payload = {"job_id": agent_job_id, "project": job["project"], "map": job["map"],
               "config": job["config"], "sequences": [job["sequence"]],
               # one start time for every computer of a shot, so {date}/{time} in file names match
               "init_time": shot_start_time(job)}
    if job["frame_start"] is not None:
        payload.update(frame_start=job["frame_start"], frame_end=job["frame_end"])
    if job["warmup"]:
        payload["warmup"] = job["warmup"]
    if (job["kind"] or "render") != "render":
        payload["kind"] = job["kind"]
    shared_ddc = get_setting("shared_ddc")
    if shared_ddc:
        payload["shared_ddc"] = shared_ddc
    output_dir = output_dir_for(job) if (job["kind"] or "render") == "render" else ""
    if output_dir:
        payload["output_dir"] = output_dir
    if get_setting("fast_mode"):
        payload["fast_mode"] = True
    split_now = job["split_mode"] == "auto"
    if split_now:
        job = split_into_pieces(job, auto_pieces(job))
    if job["split_mode"] in ("auto", "auto-piece") and job["frame_start"] is None:
        payload["auto_split"] = {"pieces": job["chunk_count"], "index": job["chunk_index"],
                                 "min_chunk": AUTO_MIN_CHUNK}
    label = task_label(job["sequence"], job["frame_start"], job["frame_end"])
    try:
        r = HTTP.post(agent_url(info, "/render"), json=payload, headers=agent_headers(), timeout=5)
    except requests.RequestException as e:
        logger.warning("Could not reach %s for queue #%s: %s", node, job["id"], e)
        return False  # stays queued; the node shows offline on the next poll
    if r.status_code == 409:
        return False  # busy after all; try again next pass

    tried = json.dumps(json.loads(job["tried_nodes"]) + [node])
    cancelled_meanwhile = False
    with DB_LOCK, connect_db() as conn, conn:
        conn.execute("UPDATE jobs SET dispatches=? WHERE id=?", (dispatch_no, job["id"]))
        if r.ok:
            cancelled_meanwhile = not conn.execute(
                "UPDATE jobs SET status='ASSIGNED', node_name=?, agent_job_id=?, attempts=?, tried_nodes=?, "
                "assigned_at=?, detail='', updated_at=? WHERE id=? AND status='QUEUED'",
                (node, agent_job_id, attempt, tried, time.time(), now_text(), job["id"])).rowcount
            if not cancelled_meanwhile:
                _insert_history(conn, node, job["project"], label, "DISPATCHED",
                                detail=f"queue #{job['id']}, attempt {attempt}/{job['max_attempts']}")
            elif split_now:
                # cancelled while being sent: the pieces made for it must not render either
                conn.execute("UPDATE jobs SET status='CANCELLED', detail='Cancelled', updated_at=? "
                             "WHERE shot_id=? AND status='QUEUED'", (now_text(), job["shot_id"]))
        else:
            try:
                body = r.json()
                reason = body.get("error", r.reason) if isinstance(body, dict) else r.reason
            except ValueError:
                reason = r.reason
            _insert_history(conn, node, job["project"], label, "REJECTED",
                            detail=f"HTTP {r.status_code}: {reason}")
            current = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if current["status"] == "QUEUED":  # not cancelled while it was being sent
                _reject_on_node(conn, current, node, r.status_code, reason, attempt, tried)
    if cancelled_meanwhile:
        _send_cancel(node)  # the job was cancelled while it was being sent
        return False
    if r.ok:
        pending = {**cached_status(node), "stage": "INITIALIZING"}
        with STATUS_LOCK:
            STATUS_CACHE[node] = pending
    return r.ok


def _reject_on_node(conn, job, node, status_code, reason, attempt, tried):
    """A computer refused a job. A 4xx means this computer can't run it (project outside its folders,
    old agent, bad path there): skip that computer without using up a try. Otherwise it counts."""
    if 400 <= status_code < 500:
        avoid = sorted(set(json.loads(job["avoid_nodes"] or "[]")) | {node})
        conn.execute("UPDATE jobs SET avoid_nodes=?, tried_nodes=? WHERE id=?", (json.dumps(avoid), tried, job["id"]))
        with NODES_LOCK:
            registered = set(RENDER_NODES)
        allowed = [n for n in (json.loads(job["allowed_nodes"]) or sorted(registered)) if n in registered]
        if [n for n in allowed if n not in avoid]:
            conn.execute("UPDATE jobs SET detail=?, updated_at=? WHERE id=?",
                         (f"{node} can't render this ({reason}); waiting for another computer"[:1000], now_text(), job["id"]))
        else:
            conn.execute("UPDATE jobs SET status='FAILED', node_name='', detail=?, updated_at=? WHERE id=?",
                         (f"No chosen computer can render this. {node}: {reason}"[:1000], now_text(), job["id"]))
        return
    conn.execute("UPDATE jobs SET attempts=?, tried_nodes=? WHERE id=?", (attempt, tried, job["id"]))
    refreshed = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    _requeue_or_fail(conn, refreshed, f"Rejected by {node} (HTTP {status_code}): {reason}")


def shot_start_time(job):
    """When the shot was sent (the same for all its pieces): every computer stamps {date}/{time} from it"""
    with connect_db() as conn:
        row = conn.execute("SELECT MIN(created_at) FROM jobs WHERE shot_id=?", (job["shot_id"],)).fetchone()
    return (row[0] if row and row[0] else job["created_at"]) or now_text()


def schedule_jobs(now=None):
    """Hand queued jobs to idle nodes, one job per node"""
    with SCHEDULER_LOCK:
        requeue_lost_jobs(now)
        with NODES_LOCK:
            registered = dict(RENDER_NODES)
        with connect_db() as conn:
            queued = conn.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY priority, id").fetchall()
            assigned_nodes = {row["node_name"] for row in
                              conn.execute("SELECT node_name FROM jobs WHERE status='ASSIGNED'")}
        if not queued:
            return
        note_jobs_waiting_for_new_agents(queued, registered)
        for node in sorted(registered):
            if node in assigned_nodes or cached_status(node).get("stage") != "IDLE":
                continue
            job = pick_job(node, queued, registered)
            if job and dispatch_job(job, node, registered[node]):
                queued.remove(job)
            if not queued:
                break


def poller_loop():
    while True:
        try:
            poll_all_nodes()
            schedule_jobs()
        except Exception:
            logger.exception("Polling/scheduling pass failed")
        WAKE_SCHEDULER.wait(POLL_INTERVAL)
        WAKE_SCHEDULER.clear()


# ------------------------------
# DASHBOARD AUTH & REQUEST HARDENING
# ------------------------------
def _basic_auth_ok():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    user_ok = hmac.compare_digest(user.encode(), DASH_USER.encode())
    pass_ok = hmac.compare_digest(password.encode(), DASH_PASSWORD.encode())
    return user_ok and pass_ok


@app.before_request
def require_login():
    if request.path == "/healthz":
        return None  # liveness probe for Docker; reveals nothing
    if request.path == "/register-node":
        # Called by render agents themselves: authenticated with the farm token, not the dashboard login
        if not FARM_TOKEN:
            return jsonify({"error": "master has no URF_FARM_TOKEN configured"}), 503
        supplied = request.headers.get("X-Farm-Token", "")
        if not hmac.compare_digest(supplied.encode(), FARM_TOKEN.encode()):
            return jsonify({"error": "unauthorized"}), 401
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None
    if not DASH_PASSWORD:
        return jsonify({"error": "dashboard has no URF_DASH_PASSWORD configured"}), 503
    if not _basic_auth_ok():
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Unreal Render Farm"'})
    if request.method == "POST":
        # Block cross-site requests riding on the browser's saved login
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return jsonify({"error": "cross-origin request refused"}), 403
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415


def _ui_build():
    """A short fingerprint of the dashboard files; changes whenever they are updated"""
    import hashlib
    digest = hashlib.sha1()
    for name in ("index.html", "dashboard.js", "dashboard.css"):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                digest.update(f.read())
        except OSError:
            pass
    return digest.hexdigest()[:12]


UI_BUILD = _ui_build()


@app.after_request
def security_headers(resp):
    resp.headers["X-Farm-UI"] = UI_BUILD
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------
# API ROUTES
# ------------------------------
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok"})


@app.route('/favicon.ico')
def favicon():
    return Response(status=204)


@app.route('/get-nodes')
def get_nodes():
    with NODES_LOCK:
        return jsonify(dict(RENDER_NODES))


@app.route('/get-history')
def get_history():
    """Newest first. ?q= searches node/project/sequence/status/detail; ?dispatched=1 includes dispatch events."""
    q = (request.args.get("q") or "").strip()[:100]
    include_dispatched = request.args.get("dispatched") == "1"
    limit = max(1, min(request.args.get("limit", default=200, type=int), 1000))
    where, params = [], []
    if not include_dispatched:
        where.append("status != 'DISPATCHED'")
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in
                                       ("node_name", "project", "sequence", "status", "detail")) + ")")
        params += [like] * 5
    sql = ("SELECT timestamp, node_name, project, sequence, status, duration, frames_rendered, detail, saved_to "
           "FROM history"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?")
    with connect_db() as conn:
        rows = conn.execute(sql, params + [limit]).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/add-node', methods=['POST'])
def add_node():
    data = json_body()
    name = text_field(data, "name")
    ip = text_field(data, "ip")
    error = validate_node(name, ip)
    if error:
        return jsonify({"error": error}), 400
    with NODES_LOCK:
        if name in RENDER_NODES:
            return jsonify({"error": f"Node '{name}' is already registered. Remove it first to change its IP."}), 409
        RENDER_NODES[name] = {"ip": ip}
        save_nodes(RENDER_NODES)
    return jsonify({"status": "added"})


@app.route('/register-node', methods=['POST'])
def register_node():
    """A render agent announces itself (name + LAN IP). Lets nodes join without manual registration."""
    data = json_body()
    name = text_field(data, "name")
    ip = text_field(data, "ip")
    error = validate_node(name, ip)
    if error:
        return jsonify({"error": error}), 400
    with NODES_LOCK:
        existing = RENDER_NODES.get(name)
        unchanged = bool(existing and existing.get("ip") == ip)
    if unchanged:
        return jsonify({"status": "unchanged", "reachable": master_can_reach(ip)})
    with NODES_LOCK:
        existing = RENDER_NODES.get(name)
        if existing and cached_status(name).get("stage") not in OFFLINE_STAGES:
            # Same name, different IP, and the registered machine still answers: refuse rather than hijack
            return jsonify({"error": f"'{name}' is already registered at {existing['ip']}, which is online"}), 409
        RENDER_NODES[name] = {"ip": ip}
        save_nodes(RENDER_NODES)
        with STATUS_LOCK:
            STATUS_CACHE.pop(name, None)
    logger.info("Node %s %s at %s", name, "moved" if existing else "registered itself", ip)
    WAKE_SCHEDULER.set()
    return jsonify({"status": "updated" if existing else "registered", "reachable": master_can_reach(ip)})


def master_can_reach(ip):
    """Can the master open the node's agent port? (A firewall on the node is the usual reason it can't.)"""
    try:
        HTTP.get(agent_url({"ip": ip}, "/health"), timeout=3)
        return True
    except requests.RequestException:
        return False


@app.route('/remove-node', methods=['POST'])
def remove_node():
    name = json_body().get("name")
    with NODES_LOCK:
        known = name in RENDER_NODES
    if not known:
        return jsonify({"error": "Node not found"}), 404
    if cached_status(name).get("stage") in ("INITIALIZING", "RENDERING"):
        _send_cancel(name)  # otherwise it keeps rendering frames another computer is about to render
    with NODES_LOCK:
        if name not in RENDER_NODES:
            return jsonify({"error": "Node not found"}), 404
        del RENDER_NODES[name]
        save_nodes(RENDER_NODES)
    with STATUS_LOCK:
        STATUS_CACHE.pop(name, None)
    WAKE_SCHEDULER.set()  # its job (if any) goes back to the queue
    return jsonify({"status": "removed"})


@app.route('/launch', methods=['POST'])
def launch():
    """Queue a batch. Each sequence becomes one job, or several frame-range jobs when chunk_size is set.

    sequences: ["/Game/Seq/A", ...] or [{"path": "/Game/Seq/A", "frames": "0-1000"}, ...]
    """
    d = json_body()
    project = clean_project(d.get("project"))
    map_path, config = clean_asset(d.get("map")), clean_asset(d.get("config"))
    raw = d.get("sequences")
    if isinstance(raw, str):
        raw = raw.split(",")
    items = []
    for item in raw or []:
        if isinstance(item, str) and clean_asset(item):
            items.append((clean_asset(item), None))
        elif isinstance(item, dict) and clean_asset(item.get("path")):
            items.append((clean_asset(item["path"]), item.get("frames")))
    if project and not project.lower().endswith(".uproject"):
        return jsonify({"error": "Project file must be the .uproject file itself, e.g. N:\\Projects\\Film\\Film.uproject"}), 400
    nodes = d.get("nodes", [])
    if not isinstance(nodes, list):
        return jsonify({"error": "nodes must be a list of computer names"}), 400
    selected = [n for n in nodes if isinstance(n, str)]
    output_dir = clean_output_dir(d.get("output_dir"))
    if output_dir and (len(output_dir) > 400 or not OUTPUT_DIR_RE.match(output_dir)):
        return jsonify({"error": "Save frames to must be a shared folder like \\\\server\\share\\Renders "
                                 "or K:\\Renders"}), 400
    prepare = d.get("prepare") or ""
    if prepare not in ("", "quick", "whole"):
        return jsonify({"error": "prepare must be 'quick' or 'whole'"}), 400
    if prepare == "whole":
        # Epic's -run=DerivedDataCache -fill: the whole project, no map or sequence needed
        map_path, config = map_path or "/Game/None", config or "/Game/None"
        items = [("WholeProject", None)]

    if not (project and map_path and config):
        return jsonify({"error": "project, map and config are required"}), 400
    if not items:
        return jsonify({"error": "no sequences to render"}), 400
    try:
        priority = int(d.get("priority", 1))
        retries = int(d.get("retries", 2))
        chunk_size = int(d.get("chunk_size") or 0)
        # Share by default (as the form does); only an explicit "auto_split": false renders each shot whole.
        # A dashboard page opened before the Share box existed sends nothing and still gets sharing.
        auto_split = d.get("auto_split", True)
        auto_split = auto_split.strip().lower() not in ("false", "0", "no", "off") if isinstance(auto_split, str) \
            else bool(auto_split)
        warmup = int(d.get("warmup", 8 if (chunk_size or auto_split) else 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "priority, retries, chunk_size and warmup must be numbers"}), 400
    if priority not in PRIORITIES or not 0 <= retries <= MAX_RETRIES:
        return jsonify({"error": f"priority must be 0-2 and retries 0-{MAX_RETRIES}"}), 400
    if chunk_size < 0 or not 0 <= warmup <= 200:
        return jsonify({"error": "chunk_size must be 0 or more and warmup 0-200"}), 400

    with NODES_LOCK:
        unknown = [n for n in selected if n not in RENDER_NODES]
    if unknown:
        return jsonify({"error": f"unknown nodes: {', '.join(unknown)}"}), 400

    batch_id = uuid.uuid4().hex[:12]
    stamp = now_text()
    rows = []
    for sequence, frames in items:
        try:
            frame_range = parse_frame_range(frames)
        except ValueError as e:
            return jsonify({"error": f"{sequence}: {e}"}), 400
        shot_id = uuid.uuid4().hex[:12]
        if prepare:
            # Warm the cache before the real render: never split, Rush so it runs first
            rows.append((batch_id, project, map_path, config, sequence, 0, retries + 1, json.dumps(selected),
                         stamp, stamp, None, None, 0, shot_id, 1, 1, "",
                         "prepare-fill" if prepare == "whole" else "prepare"))
            continue
        if auto_split and not frame_range:
            # Shared automatically: the first computer reads the shot's frames and plans the pieces
            rows.append((batch_id, project, map_path, config, sequence, priority, retries + 1,
                         json.dumps(selected), stamp, stamp, None, None, warmup, shot_id, 1, 1, "auto", "render"))
            continue
        if auto_split and frame_range and not chunk_size:
            # Frames typed and sharing ticked: even pieces, one per free computer, none under the minimum
            pieces = auto_pieces({"allowed_nodes": json.dumps(selected)}, feature="frame_range")
            chunks = even_pieces(*frame_range, pieces, AUTO_MIN_CHUNK)
        else:
            # No frame range = the whole shot on one computer, even when other shots in the batch are split
            chunks = split_frames(*frame_range, chunk_size) if frame_range else [(None, None)]
        if len(chunks) > MAX_CHUNKS_PER_SHOT:
            return jsonify({"error": f"{sequence}: {len(chunks)} chunks; use a bigger chunk size "
                                     f"(max {MAX_CHUNKS_PER_SHOT} per shot)"}), 400
        for index, (start, end) in enumerate(chunks, 1):
            rows.append((batch_id, project, map_path, config, sequence, priority, retries + 1,
                         json.dumps(selected), stamp, stamp, start, end, warmup if start is not None else 0,
                         shot_id, index, len(chunks), "", "render"))
    if len(rows) > MAX_JOBS_PER_LAUNCH:
        return jsonify({"error": f"{len(rows)} tasks in one batch; the limit is {MAX_JOBS_PER_LAUNCH}"}), 400

    with DB_LOCK, connect_db() as conn, conn:
        conn.executemany(
            "INSERT INTO jobs (batch_id, project, map, config, sequence, priority, max_attempts, "
            "allowed_nodes, created_at, updated_at, frame_start, frame_end, warmup, shot_id, "
            "chunk_index, chunk_count, split_mode, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if output_dir:
            conn.execute("UPDATE jobs SET output_dir=? WHERE batch_id=?", (output_dir, batch_id))
    WAKE_SCHEDULER.set()
    sharing = {}
    if prepare:
        usable, unable = share_candidates(json.dumps(selected), "prepare")
        return jsonify({"status": "queued", "batch_id": batch_id, "shots": len(items), "jobs": len(rows),
                        "prepare": prepare, "computers": usable,
                        "left_out": [{"node": n, "why": why} for n, why in unable]})
    if auto_split:
        typed = any(frames for _, frames in items)
        usable, unable = share_candidates(json.dumps(selected), "frame_range" if typed else "auto_piece")
        sharing = {"computers": usable, "left_out": [{"node": n, "why": why} for n, why in unable]}
    return jsonify({"status": "queued", "batch_id": batch_id, "shots": len(items), "jobs": len(rows),
                    "auto": sum(1 for r in rows if r[-2] == "auto"), "sharing": sharing,
                    "sharing_off": not auto_split})


@app.route('/get-queue')
def get_queue():
    if request.args.get("summary"):
        with connect_db() as conn:
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        return jsonify({"jobs": [], "shots": [], "counts": counts})
    with connect_db() as conn:
        active = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('ASSIGNED','QUEUED') "
            "ORDER BY status='QUEUED', priority, id").fetchall()
        finished = conn.execute(
            "SELECT * FROM jobs WHERE status NOT IN ('ASSIGNED','QUEUED') ORDER BY updated_at DESC, id DESC LIMIT 100"
        ).fetchall()
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        shot_ids = sorted({row["shot_id"] for row in active + finished if row["chunk_count"] > 1})
        shots = []
        if shot_ids:
            marks = ",".join("?" * len(shot_ids))
            shots = [dict(r) for r in conn.execute(
                "SELECT shot_id, MIN(sequence) AS sequence, MIN(frame_start) AS frame_start, "
                "MAX(frame_end) AS frame_end, COUNT(*) AS chunks, SUM(status='SUCCESS') AS done, "
                "SUM(status='ASSIGNED') AS rendering, SUM(status='QUEUED') AS queued, "
                "SUM(status='FAILED') AS failed, SUM(status='CANCELLED') AS cancelled, "
                "SUM(CASE WHEN status='SUCCESS' THEN frames_written ELSE 0 END) AS frames_written, "
                "SUM(CASE WHEN detail LIKE 'Not needed:%' THEN 0 ELSE frame_end - frame_start + 1 END) AS frames_expected, "
                "SUM(frame_start IS NULL AND detail NOT LIKE 'Not needed:%') AS unplanned, "
                "MAX(saved_to) AS saved_to, "
                f"MAX(updated_at) AS updated_at FROM jobs WHERE shot_id IN ({marks}) "
                "GROUP BY shot_id ORDER BY MIN(id) DESC", shot_ids)]
    fields = ("id", "batch_id", "project", "sequence", "priority", "status", "attempts",
              "max_attempts", "node_name", "detail", "created_at", "updated_at",
              "frame_start", "frame_end", "shot_id", "chunk_index", "chunk_count", "split_mode", "kind",
              "frames_written")
    jobs = []
    running = {}  # shot_id -> summed progress (0-1) of its rendering chunks
    for row in active + finished:
        job = {k: row[k] for k in fields}
        job["progress"] = live_progress(row)
        job["saved_to"], job["saved_to_kind"] = saved_to_for(row)
        if job["progress"] is not None:
            running[row["shot_id"]] = running.get(row["shot_id"], 0) + job["progress"] / 100
        jobs.append(job)
    for shot in shots:
        done = (shot["done"] or 0) + running.get(shot["shot_id"], 0)
        shot["percent"] = round(done / shot["chunks"] * 100, 1) if shot["chunks"] else 0
        if shot.pop("unplanned"):
            shot["frames_expected"] = None  # some pieces do not know their frames yet
    return jsonify({"jobs": jobs, "shots": shots, "counts": counts})


def live_progress(job):
    """Percent done of a job that is rendering right now (from its node's latest status), else None"""
    if job["status"] != "ASSIGNED":
        return None
    node = cached_status(job["node_name"])
    if node.get("job_id") == job["agent_job_id"] and node.get("stage") == "RENDERING":
        return float(node.get("progress") or 0)
    return 0.0


@app.route('/cancel-shot', methods=['POST'])
def cancel_shot():
    """Cancel every queued or rendering chunk of a split shot"""
    shot_id = json_body().get("shot_id")
    if not isinstance(shot_id, str) or not shot_id:
        return jsonify({"error": "shot_id is required"}), 400
    with DB_LOCK, connect_db() as conn, conn:
        queued = conn.execute("UPDATE jobs SET status='CANCELLED', detail='Cancelled before it started', "
                              "updated_at=? WHERE shot_id=? AND status='QUEUED'", (now_text(), shot_id)).rowcount
        rendering = [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE shot_id=? AND status='ASSIGNED'", (shot_id,))]
    for job_id in rendering:
        cancel_job(job_id)
    return jsonify({"status": "cancelled", "jobs": queued + len(rendering)})


def _send_cancel(node_name):
    with NODES_LOCK:
        info = dict(RENDER_NODES[node_name]) if node_name in RENDER_NODES else None
    if not info:
        return False
    try:
        r = HTTP.post(agent_url(info, "/cancel"), headers=agent_headers(), timeout=5)
        return r.ok
    except requests.RequestException:
        return False


def cancel_job(job_id):
    """Cancel a queued job, or stop it on its node. Returns (ok, message)."""
    with DB_LOCK, connect_db() as conn, conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return False, "Job not found"
        if job["status"] == "QUEUED":
            conn.execute("UPDATE jobs SET status='CANCELLED', detail='Cancelled before it started', updated_at=? "
                         "WHERE id=?", (now_text(), job_id))
            return True, "cancelled"
        if job["status"] != "ASSIGNED":
            return False, f"Job is already {job['status']}"
        conn.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (now_text(), job_id))
        node = job["node_name"]
    # The agent reports CANCELLED itself; if the node can't be reached, close the job here
    if _send_cancel(node):
        return True, "cancelling"
    with DB_LOCK, connect_db() as conn, conn:
        conn.execute("UPDATE jobs SET status='CANCELLED', detail=?, updated_at=? WHERE id=? AND status='ASSIGNED'",
                     (f"Cancelled; {node} could not be reached", now_text(), job_id))
    return True, "cancelled (node unreachable)"


FINISHED = ("SUCCESS", "FAILED", "CANCELLED")


@app.route('/clear-queue', methods=['POST'])
def clear_queue():
    """Remove finished jobs (done, failed, cancelled) from the queue. History keeps them. Jobs waiting or
    rendering stay, and so does every piece of a shared shot that still has pieces waiting or rendering
    (its progress bar counts all its pieces)."""
    marks = ",".join("?" * len(FINISHED))
    with DB_LOCK, connect_db() as conn, conn:
        removed = conn.execute(
            f"DELETE FROM jobs WHERE status IN ({marks}) AND shot_id NOT IN "
            "(SELECT shot_id FROM jobs WHERE status IN ('QUEUED','ASSIGNED') AND shot_id != '')",
            FINISHED).rowcount
    logger.info("Cleared %s finished job(s) from the queue", removed)
    return jsonify({"status": "cleared", "removed": removed})


@app.route('/cancel-job', methods=['POST'])
def cancel_job_route():
    job_id = json_body().get("id")
    if not isinstance(job_id, int):
        return jsonify({"error": "id must be a job number"}), 400
    ok, message = cancel_job(job_id)
    return (jsonify({"status": message}), 200) if ok else (jsonify({"error": message}), 409)


JOB_SETTINGS = ("id", "status", "detail", "project", "map", "config", "sequence", "frame_start", "frame_end",
                "priority", "max_attempts", "split_mode", "shot_id", "chunk_index", "chunk_count", "warmup", "kind",
                "output_dir")


def job_settings(row):
    data = {k: row[k] for k in JOB_SETTINGS}
    data["allowed_nodes"] = json.loads(row["allowed_nodes"] or "[]")
    data["retries"] = max(0, row["max_attempts"] - 1)
    data["shared"] = row["split_mode"] in ("auto", "auto-split") or row["chunk_count"] > 1
    data["is_piece"] = row["chunk_count"] > 1
    return data


@app.route('/get-job')
def get_job():
    """Current settings of one job (?id=) or of a shared shot's first failed piece (?shot_id=), for Retry"""
    with connect_db() as conn:
        if request.args.get("shot_id"):
            row = conn.execute(
                "SELECT * FROM jobs WHERE shot_id=? ORDER BY status NOT IN ('FAILED','CANCELLED'), id LIMIT 1",
                (request.args["shot_id"],)).fetchone()
        else:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (request.args.get("id", type=int),)).fetchone()
    if not row:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job_settings(row))


def retry_changes(d, single):
    """Validate settings changed in the Retry window. Returns (column -> value, error).
    single=False (a piece of a shared shot): the shot and its frames stay as they are."""
    cols = {}
    if "project" in d:
        project = clean_project(d["project"])
        if not project.lower().endswith(".uproject"):
            return None, "Project file must be the .uproject file itself"
        cols["project"] = project
    for key in ("map", "config"):
        if key in d:
            value = clean_asset(d[key])
            if not value:
                return None, f"{key} cannot be empty"
            cols[key] = value
    if "output_dir" in d:
        output_dir = clean_output_dir(d["output_dir"])
        if output_dir and (len(output_dir) > 400 or not OUTPUT_DIR_RE.match(output_dir)):
            return None, "Save frames to must be a shared folder like \\\\server\\share\\Renders or K:\\Renders"
        cols["output_dir"] = output_dir
    if "priority" in d:
        if d["priority"] not in PRIORITIES:
            return None, "priority must be 0, 1 or 2"
        cols["priority"] = d["priority"]
    if "retries" in d:
        if not isinstance(d["retries"], int) or not 0 <= d["retries"] <= MAX_RETRIES:
            return None, f"retries must be 0-{MAX_RETRIES}"
        cols["max_attempts"] = d["retries"] + 1
    if "nodes" in d:
        nodes = [n for n in d["nodes"] if isinstance(n, str)] if isinstance(d["nodes"], list) else None
        with NODES_LOCK:
            unknown = [n for n in nodes or [] if n not in RENDER_NODES]
        if nodes is None or unknown:
            return None, f"unknown computers: {', '.join(unknown)}" if unknown else "nodes must be a list"
        if not nodes:
            return None, "pick at least one computer"
        cols["allowed_nodes"] = json.dumps(nodes)
    if single:
        if "sequence" in d:
            sequence = clean_asset(d["sequence"])
            if not sequence:
                return None, "shot cannot be empty"
            cols["sequence"] = sequence
        if "frames" in d or "share" in d:
            try:
                frame_range = parse_frame_range(d.get("frames"))
            except ValueError as e:
                return None, str(e)
            if frame_range:
                cols.update(frame_start=frame_range[0], frame_end=frame_range[1], split_mode="")
            elif d.get("share"):
                cols.update(frame_start=None, frame_end=None, split_mode="auto")
            else:
                cols.update(frame_start=None, frame_end=None, split_mode="")
    return cols, None


def _requeue(conn, where, params, cols, note):
    sets = ["status='QUEUED'", "attempts=0", "network_retries=0", "tried_nodes='[]'", "avoid_nodes='[]'",
            "cancel_requested=0", "node_name=''",
            "detail=?", "updated_at=?"] + [f"{c}=?" for c in cols]
    values = [note, now_text()] + list(cols.values())
    if cols.get("split_mode") == "auto":
        sets.append("warmup=MAX(warmup, 8)")
    return conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE {where} AND status IN ('FAILED','CANCELLED')",
                        values + list(params)).rowcount


@app.route('/retry-job', methods=['POST'])
def retry_job():
    """Re-queue a failed or cancelled job, optionally with changed settings ("changes": {...})"""
    d = json_body()
    job_id = d.get("id")
    if not isinstance(job_id, int):
        return jsonify({"error": "id must be a job number"}), 400
    with DB_LOCK, connect_db() as conn, conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] not in ("FAILED", "CANCELLED"):
            return jsonify({"error": "Only failed or cancelled jobs can be retried"}), 409
        cols, error = retry_changes(d.get("changes") or {}, single=row["chunk_count"] <= 1)
        if error:
            return jsonify({"error": error}), 400
        changed = [c for c, v in cols.items() if row[c] != v]
        note = "Retried with new " + ", ".join(sorted(set(c.replace("_", " ") for c in changed))) if changed \
            else "Manually re-queued"
        _requeue(conn, "id=?", (job_id,), cols, note)
    WAKE_SCHEDULER.set()
    return jsonify({"status": "queued", "changed": changed})


@app.route('/retry-shot', methods=['POST'])
def retry_shot():
    """Re-queue the failed or cancelled pieces of a shared shot (like Deadline's 'requeue failed tasks'),
    optionally with changed project/map/preset/priority/retries/computers for all of them"""
    d = json_body()
    shot_id = d.get("shot_id")
    if not isinstance(shot_id, str) or not shot_id:
        return jsonify({"error": "shot_id is required"}), 400
    cols, error = retry_changes(d.get("changes") if isinstance(d.get("changes"), dict) else {}, single=False)
    if error:
        return jsonify({"error": error}), 400
    note = "Retried with new settings" if cols else "Manually re-queued"
    with DB_LOCK, connect_db() as conn, conn:
        updated = _requeue(conn, "shot_id=?", (shot_id,), cols, note)
    if updated:
        WAKE_SCHEDULER.set()
    return jsonify({"status": "queued", "jobs": updated})


@app.route('/get-settings')
def get_settings():
    return jsonify({key: get_setting(key) for key in SETTING_DEFAULTS})


@app.route('/save-settings', methods=['POST'])
def save_settings():
    d = json_body()
    if "shared_ddc" not in d and "output_root" not in d and "fast_mode" not in d:
        return jsonify({"error": "nothing to save"}), 400
    saved = {}
    if "fast_mode" in d:
        if not isinstance(d["fast_mode"], bool):
            return jsonify({"error": "fast_mode must be true or false"}), 400
        with DB_LOCK, connect_db() as conn, conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('fast_mode', ?)",
                         ("1" if d["fast_mode"] else "",))
        logger.info("Fast mode %s", "on" if d["fast_mode"] else "off")
        saved["fast_mode"] = "1" if d["fast_mode"] else ""
        if "shared_ddc" not in d and "output_root" not in d:
            return jsonify({"status": "saved", **saved})
    if "output_root" in d:
        root = clean_output_dir(d.get("output_root"))
        if root and (len(root) > 300 or not OUTPUT_DIR_RE.match(root)):
            return jsonify({"error": "The output folder must be a shared folder like \\\\server\\share\\Renders "
                                     "or a drive letter every render computer has, like K:\\Renders"}), 400
        with DB_LOCK, connect_db() as conn, conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('output_root', ?)", (root,))
        logger.info("Farm output folder set to %r", root)
        saved["output_root"] = root
    if "shared_ddc" not in d:
        return jsonify({"status": "saved", **saved})
    path = clean_cache_path(d.get("shared_ddc"))
    if path and (len(path) > 260 or not UNC_PATH_RE.match(path)):
        return jsonify({"error": "The shared cache must be a network folder like "
                                 "\\\\192.168.1.20\\RenderCache\\FarmDDC (not a drive letter: "
                                 "the render computers may map drives differently)"}), 400
    with DB_LOCK, connect_db() as conn, conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shared_ddc', ?)", (path,))
    logger.info("Shared cache folder set to %r", path)
    return jsonify({"status": "saved", "shared_ddc": path, **saved})


@app.route('/check-cache', methods=['POST'])
def check_cache():
    """Ask every online computer whether it can write to the shared cache folder"""
    path = clean_cache_path(json_body().get("path")) or get_setting("shared_ddc")
    if not path:
        return jsonify({"error": "Set the shared cache folder first"}), 400
    if len(path) > 260 or not UNC_PATH_RE.match(path):
        return jsonify({"error": "The shared cache must be a network folder like \\\\server\\share\\FarmDDC"}), 400
    with NODES_LOCK:
        nodes = dict(RENDER_NODES)

    def check(item):
        name, info = item
        if cached_status(name).get("stage") in OFFLINE_STAGES:
            return {"node": name, "ok": False, "error": "offline"}
        if not can_split(name, "shared_ddc"):
            return {"node": name, "ok": False, "error": "needs the latest SETUP.bat"}
        try:
            r = HTTP.post(agent_url(info, "/check-cache"), json={"path": path}, headers=agent_headers(), timeout=10)
            data = r.json()
            return {"node": name, "ok": bool(data.get("ok")), "error": data.get("error", "")}
        except (requests.RequestException, ValueError) as e:
            return {"node": name, "ok": False, "error": f"no answer: {str(e)[:120]}"}

    if not nodes:
        return jsonify({"path": path, "results": []})
    with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as pool:
        results = list(pool.map(check, sorted(nodes.items())))
    return jsonify({"path": path, "results": results})


@app.route('/cancel-node', methods=['POST'])
def cancel_node():
    name = json_body().get("node")
    with NODES_LOCK:
        known = name in RENDER_NODES
    if not known:
        return jsonify({"error": "Node not found"}), 404
    with connect_db() as conn:
        job = conn.execute("SELECT id FROM jobs WHERE status='ASSIGNED' AND node_name=?", (name,)).fetchone()
    if job:
        ok, message = cancel_job(job["id"])
        return jsonify({"status": message})
    # Nothing from the queue on this node, but stop whatever it is rendering
    return (jsonify({"status": "cancelling"}), 200) if _send_cancel(name) else (jsonify({"status": "offline"}), 502)


@app.route('/status')
def status():
    """Latest status of every registered node (refreshed by the background poller)"""
    with NODES_LOCK:
        names = sorted(RENDER_NODES)
    results = []
    for name in names:
        data = dict(cached_status(name))
        for internal in ("boot_id", "project"):
            data.pop(internal, None)
        if data.get("offline_since"):
            data["offline_since"] = datetime.fromtimestamp(data["offline_since"]).isoformat(timespec="seconds")
        data["node"] = name
        data["output_folder"] = clean_saved_to(data.get("output_folder"))
        data["output_folder_kind"] = "live" if data["output_folder"] else ""
        if not data["output_folder"] and data.get("job_id") and data.get("stage") in ("INITIALIZING", "RENDERING"):
            with connect_db() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE agent_job_id=? AND status='ASSIGNED'",
                                   (data["job_id"],)).fetchone()
            if row:
                data["output_folder"], data["output_folder_kind"] = saved_to_for(row)
        results.append(data)
    return jsonify(results)


def setup_logging():
    # Logs redirected to a file/service use the ANSI codepage, which can't encode every character
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    handlers = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


if __name__ == "__main__":
    setup_logging()
    for var, value in (("URF_FARM_TOKEN", FARM_TOKEN), ("URF_DASH_PASSWORD", DASH_PASSWORD)):
        if len(value) < MIN_SECRET_LENGTH or value.lower().startswith("change-me"):
            raise SystemExit(f"Set {var} to your own secret of at least {MIN_SECRET_LENGTH} characters "
                             "(not the example value from farm.env.example).")

    init_storage()
    threading.Thread(target=poller_loop, daemon=True).start()

    logger.info("=" * 60)
    logger.info("🚀 Unreal Render Farm v7.0 - Master Server")
    logger.info("📡 Dashboard: http://localhost:%s  (login: %s)", PORT, DASH_USER)
    logger.info("📊 Registered Nodes: %s", len(RENDER_NODES))
    logger.info("=" * 60)

    try:
        from waitress import serve
        serve(app, host=BIND_HOST, port=PORT, threads=16)
    except ImportError:
        app.run(host=BIND_HOST, port=PORT, threaded=True, debug=False)
