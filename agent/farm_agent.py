from flask import Flask, request, jsonify
from functools import wraps
import collections
import hmac
import subprocess
import threading
import shutil
import socket
import tempfile
import os
import time
import re
import sys
import uuid
import json
import logging
import urllib.error
import urllib.request
import psutil
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

UNREAL_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unreal")
sys.path.insert(0, UNREAL_SCRIPTS_DIR)
from urf_mrq_common import parse_tagged_line  # noqa: E402  (shared with the in-Unreal executor)

app = Flask(__name__)
logger = logging.getLogger("farm.agent")


def log(message):
    """Log a line; never raises, even on a console that can't encode Unreal's output (e.g. cp1252)"""
    logger.info("%s", message)


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

NODE_NAME = socket.gethostname()
BOOT_ID = uuid.uuid4().hex  # lets the master detect agent restarts (result numbering resets)

# ------------------------------
# CONFIGURATION (environment variables or farm.env)
# ------------------------------
LOG_FILE = os.environ.get("URF_LOG_FILE", "")
# e.g. http://192.168.1.5:5000 - when set, this node registers itself with the master
MASTER_URL = os.environ.get("URF_MASTER_URL", "").rstrip("/")
# "executor": render through our Movie Render Queue Python executor (frame ranges, exact progress).
# "legacy": plain command-line render of the whole sequence, without Python (fallback).
UE_MODE = os.environ.get("URF_UE_MODE", "executor").strip().lower()
# Reported to the master so it never sends work an older agent would silently get wrong
AGENT_VERSION = "2026.10.06.9"
FEATURES = (["frame_range", "executor", "auto_split", "auto_piece", "shared_ddc", "prepare", "frame_check",
             "output_dir"]
            if UE_MODE == "executor" else [])
# Shared Derived Data Cache (e.g. a NAS folder): meshes/shaders built once, reused by every computer.
# The master's Admin setting (sent with every job) wins over this local fallback.
SHARED_DDC = os.environ.get("URF_SHARED_DDC", "").strip()
UNC_PATH_RE = re.compile(r'^\\\\[A-Za-z0-9_.\-]+\\[^"<>|*?\r\n]+$')
# nDisplay's DisplayClusterGameEngine loads in every render of a project with the plugin on; farm
# renders never use it (it slows loading and is a known Movie Render Queue crash/resolution cause)
SKIP_NDISPLAY = os.environ.get("URF_SKIP_NDISPLAY", "1").strip().lower() not in ("0", "false", "no", "off")
PLAIN_GAME_ENGINE = "-ini:Engine:[/Script/Engine.Engine]:GameEngine=/Script/Engine.GameEngine"
# Frozen-render watchdog (minutes): no Unreal output at all while loading / no progress while rendering
# The executor prints a heartbeat at least every 30 s while Unreal's engine is ticking, so one slow
# frame (path tracing, 8K) never looks frozen; only an engine that stops ticking does.
LOAD_STALL_MINUTES = float(os.environ.get("URF_LOAD_STALL_MIN", "30"))
RENDER_STALL_MINUTES = float(os.environ.get("URF_RENDER_STALL_MIN", "20"))
# Epic's whole-project cache fill can sit quietly on one huge shader batch for a long time
FILL_STALL_MINUTES = float(os.environ.get("URF_FILL_STALL_MIN", "120"))
# The farm's script inside Unreal erroring again and again (it would otherwise leave Unreal open forever)
SCRIPT_ERROR_LIMIT = 25
# Unreal said the render is complete but the farm's script never reported: stop waiting after this
NO_RESULT_AFTER_DONE_SECONDS = 180
# After the executor reported its result, Unreal only has to close: stop it if that takes longer
RESULT_GRACE_SECONDS = 90
_progress_lock = threading.Lock()  # "never go backwards" check + update, from two reader threads
SCRIPT_ERROR_RE = re.compile(r"^\s*(\w+(?:Error|Exception)): (.+)$")
JOB_KINDS = ("render", "prepare", "prepare-fill")
# Workstation mode ("NIMBY": not in my back yard): an artist's PC renders only while nobody uses it
NIMBY = os.environ.get("URF_NIMBY", "").strip().lower() in ("1", "true", "yes", "on")
NIMBY_IDLE_MINUTES = float(os.environ.get("URF_NIMBY_IDLE_MIN", "15"))
NIMBY_EDITOR_IDLE_MINUTES = float(os.environ.get("URF_NIMBY_EDITOR_IDLE_MIN", "60"))  # owner's Unreal open
NIMBY_HOURS = os.environ.get("URF_NIMBY_HOURS", "").strip()  # e.g. 20:00-08:00; empty = any time
NIMBY_ON_RETURN = os.environ.get("URF_NIMBY_ON_RETURN", "stop").strip().lower()  # stop | finish
NIMBY_BACK_SECONDS = 30  # keyboard/mouse used this recently while rendering = the owner is back
OWNER_NOTE = ("Stopped because the owner started using this workstation. The farm renders it on another "
              "computer; this does not count as a try.")
INIT_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
# Where the farm wants the frames: a network or drive-letter folder; {sequence_name} style tokens allowed
OUTPUT_DIR_RE = re.compile(r'^(?:\\\\[A-Za-z0-9_.\-]+\\|[A-Za-z]:[\\/])[^"<>|?*\r\n]*$')
# Unreal ran out of RAM / page file / video memory: retrying on the same computer fails the same way
OUT_OF_MEMORY_RE = re.compile(r"Ran out of memory|paging file is too small|Out of video memory", re.IGNORECASE)
WHOLE_PROJECT = "WholeProject"  # the "sequence" of a whole-project prepare
REGISTER_INTERVAL = 60  # seconds
FARM_TOKEN = os.environ.get("URF_FARM_TOKEN", "")
BIND_HOST = os.environ.get("URF_AGENT_BIND", "0.0.0.0")
PORT = int(os.environ.get("URF_AGENT_PORT", "5001"))
UE_EXE = os.environ.get(
    "URF_UE_EXE",
    r"C:\Program Files\Epic Games\UE_5.6\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
)
# Semicolon-separated folders that .uproject files must live under.
# When unset, any existing local (non-UNC) .uproject path is accepted.
PROJECT_ROOTS = [
    os.path.normcase(os.path.abspath(p.strip()))
    for p in os.environ.get("URF_PROJECT_ROOTS", "").split(";") if p.strip()
]

MIN_TOKEN_LENGTH = 16
MAX_SEQUENCES = 200
MAX_FRAME = 10_000_000
RESULTS_KEPT = 500
OUTPUT_TAIL_LINES = 20
# Full Unreal output of every render, kept for troubleshooting and for tuning parse_ue_output
LOG_DIR = os.environ.get("URF_AGENT_LOG_DIR", r"C:\UnrealRenderFarm\agent\logs")
LOGS_KEPT = 200

# Unreal asset paths: /Game/Folder/Asset or /Game/Folder/Asset.Asset.
# Every segment must start with a letter, digit or underscore, so a value can never
# be read by Unreal as a command-line switch (e.g. "-ExecCmds=...").
ASSET_PATH_RE = re.compile(
    r"^/?[A-Za-z0-9_][A-Za-z0-9_\-]*(?:/[A-Za-z0-9_][A-Za-z0-9_\-]*)*(?:\.[A-Za-z0-9_][A-Za-z0-9_\-]*)?$"
)
# " -ExecCmds=..." inside a file name would reach Unreal's command-line parser
SWITCH_IN_PATH_RE = re.compile(r"\s[-/][A-Za-z][\w.]*=")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# ------------------------------
# SHARED STATE (guarded by _lock)
# ------------------------------
_lock = threading.Lock()
_cancel = threading.Event()
_process = None
_results = collections.deque(maxlen=RESULTS_KEPT)
_result_seq = 0

CURRENT_STATUS = {
    "job_id": "",
    "project": "",
    "sequence": "",
    "scene": "",
    "stage": "IDLE",
    "progress": 0,
    "current_frame": 0,
    "total_frames": 0,
    "fps": 0,
    "eta": "",
    "start_time": None,
    "last_result": None,
    "discovered": None,  # automatic sharing: the shot range + pieces the executor planned
    "activity": "",      # what Unreal is doing right now, read from its log (loading, shaders, rendering)
    "first_frame": None,  # (time, frame number) of the first frame seen, for an honest time-left estimate
}

NIMBY_STATUS = {"enabled": NIMBY, "available": None, "reason": "", "idle_minutes": None}

METRICS = {
    "cpu_usage": 0,
    "gpu_usage": None,   # None = no NVIDIA GPU / NVML unavailable
    "ram_usage": 0,
    "vram_usage": None,
}


def update_status(**kwargs):
    """Update the current status with provided key-value pairs"""
    with _lock:
        for key, value in kwargs.items():
            if key in CURRENT_STATUS:
                CURRENT_STATUS[key] = value


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


ACTIVITY_PATTERNS = [
    (re.compile(r"Waiting for static meshes to be ready (\d+)/(\d+)"), "Building meshes {0}/{1}"),
    (re.compile(r"Building static mesh|Built static mesh"), "Building meshes"),
    (re.compile(r"Building textures?:|LogTexture: Display: Building"), "Building textures"),
    (re.compile(r"(\d+)\s+shaders? (?:left|remaining)", re.IGNORECASE), "Compiling shaders: {0} left"),
    (re.compile(r"LogShaderCompilers|Compiling shader", re.IGNORECASE), "Compiling shaders"),
    (re.compile(r"LoadMap:|LogLoad: (?:Took|Game class)|Bringing World .* up for play"), "Loading the map"),
    (re.compile(r"Detected that the user intends to render a movie"), "Starting Unreal"),
    (re.compile(r"Running start-up script"), "Starting Movie Render Queue"),
    (re.compile(r"URF executor|LogMovieRenderPipeline: .*(Initializ|Warm|Started)", re.IGNORECASE),
     "Preparing the render"),
]


def detect_activity(line):
    """A short description of what Unreal is doing, from one of its log lines (or None)"""
    for pattern, text in ACTIVITY_PATTERNS:
        match = pattern.search(line)
        if match:
            return text.format(*match.groups())
    return None


def _set_progress(current, total, percent=None):
    """Update progress, ETA and fps from a frame count (and/or a percentage)"""
    if percent is None:
        percent = current / total * 100 if total else 0
    with _lock:
        start_time = CURRENT_STATUS["start_time"]
    fps = 0
    eta = "Starting..."
    now = time.time()
    with _lock:
        first = CURRENT_STATUS.get("first_frame")  # (time, frame) when frames started coming out
        if current and total and (not first or current < first[1]):
            first = CURRENT_STATUS["first_frame"] = (now, current)
    if start_time:
        done = (current / total) if (current and total) else percent / 100
        if first and current and total and current > first[1] and now > first[0]:
            # speed since the first frame: loading, shader building and warm-up are not render time
            fps = (current - first[1]) / (now - first[0])
            eta = format_duration((total - current) / fps)
            fps = round(fps, 2)
        elif done > 0 and now > start_time and not (current and total):
            eta = format_duration((now - start_time) / done * (1 - done))
        else:
            eta = "Calculating..."
    fields = {"progress": round(min(100.0, percent), 1), "fps": fps, "eta": eta}
    if current is not None and total:
        fields.update(current_frame=current, total_frames=total)
    update_status(**fields)


def seconds_per_frame():
    """Render speed of this piece from its first frame on (loading and warm-up are not counted)"""
    with _lock:
        first = CURRENT_STATUS.get("first_frame")
        current = CURRENT_STATUS.get("current_frame") or 0
    if not first or current <= first[1]:
        return None
    return (time.time() - first[0]) / (current - first[1])


PHASE_TEXT = {"warmup": "Warming up", "render": "Rendering frames", "finalize": "Writing the last frames",
              "export": "Finishing", "shutdown": "Closing"}


def describe_render_phase(data):
    """Show what Movie Render Queue is doing (warm-up, samples of a slow frame) and Unreal's own ETA"""
    phase = data.get("phase")
    if phase == "warmup" and isinstance(data.get("current"), int) and data["current"] > 0:
        phase = "render"
    if phase in PHASE_TEXT:
        text = PHASE_TEXT[phase]
        if phase == "warmup" and data.get("warmups"):
            text += f" {data.get('warmup', 0)}/{data['warmups']}"
        elif phase == "render" and isinstance(data.get("samples"), int) and data["samples"] > 1:
            text += f" (sample {data.get('sample', 0)}/{data['samples']} of this frame)"
        update_status(activity=text)
    eta = data.get("eta_seconds")
    if isinstance(eta, (int, float)) and eta >= 0:
        update_status(eta=format_duration(eta))


def parse_ue_output(line):
    """Parse Unreal Engine output for progress information. Returns True if a progress line matched."""
    # Preferred: structured progress printed by our Movie Render Queue executor
    tagged = parse_tagged_line(line)
    if not tagged and not CURRENT_STATUS["activity"].startswith(tuple(PHASE_TEXT.values())):
        activity = detect_activity(line)
        if activity:
            with _lock:
                current = CURRENT_STATUS["activity"] or ""
                # keep a counted "Building meshes 54/445" when a plain "Building meshes" line follows
                if current != activity and not current.startswith(activity + " "):
                    CURRENT_STATUS["activity"] = activity
    if tagged and tagged[0] == "progress":
        if not tagged[1].get("phase"):
            update_status(activity="Rendering frames")
        data = tagged[1]
        current, total = data.get("current"), data.get("total")
        if not (isinstance(current, int) and isinstance(total, int) and 0 <= current <= total and total > 0):
            current = total = None
        percent = float(data.get("percent") or 0)
        with _progress_lock:
            with _lock:
                shown = CURRENT_STATUS["progress"] or 0
            if percent < shown:
                return True  # an old line arriving late through Unreal's buffered stdout: keep the newer value
            _set_progress(current, total, percent)
            describe_render_phase(data)
        return True
    if tagged or UE_MODE == "executor":
        return False  # the executor reports progress itself; Unreal's own lines must not override it

    # Fallback for renders without the executor:
    # "MoviePipeline: Rendering Frame 45/120" or "Frame 45 of 120"
    frame_match = re.search(r'Frame[: ]+(\d+)\s*(?:/|of)\s*(\d+)', line)
    if frame_match:
        current = int(frame_match.group(1))
        total = int(frame_match.group(2))
        if total <= 0 or current > total:
            return False
        _set_progress(current, total)

    # Completion message only moves the bar; success is decided by the executor result / exit code
    if "MoviePipeline: Finished" in line or "Render complete" in line:
        update_status(progress=100)
    return bool(frame_match)


# ------------------------------
# RESOURCE MONITORING
# ------------------------------
def idle_seconds():
    """Seconds since the last keyboard or mouse input in this Windows session (None if unknown)"""
    try:
        import ctypes
        from ctypes import wintypes

        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        info = LastInput()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except (AttributeError, OSError):
        return None


def owner_editor_open():
    """Is the owner's own Unreal Editor running? (farm renders use UnrealEditor-Cmd.exe)"""
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() == "unrealeditor.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def within_hours(spec, now):
    """'20:00-08:00' (may cross midnight) contains now (a datetime)? An empty spec means always."""
    if not spec:
        return True
    try:
        start, end = (datetime.strptime(part.strip(), "%H:%M").time() for part in spec.split("-"))
    except ValueError:
        return True  # an unreadable window must not switch the workstation off for good
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def nimby_state(idle, editor_open, now, hours=None, idle_minutes=None, editor_idle_minutes=None):
    """(available, reason) for a workstation"""
    hours = NIMBY_HOURS if hours is None else hours
    idle_minutes = NIMBY_IDLE_MINUTES if idle_minutes is None else idle_minutes
    editor_idle_minutes = NIMBY_EDITOR_IDLE_MINUTES if editor_idle_minutes is None else editor_idle_minutes
    if not within_hours(hours, now):
        return False, f"renders only {hours}"
    if idle is None:
        return False, "can't tell whether someone is using it"
    need = editor_idle_minutes if editor_open else idle_minutes
    if idle < need * 60:
        why = "its owner's Unreal Editor is open" if editor_open else "in use by its owner"
        return False, f"{why} (joins after {need:g} min without keyboard or mouse)"
    return True, f"free (idle {int(idle // 60)} min)"


_owner_back = threading.Event()


def nimby_loop():
    """Workstation mode: switch between IDLE and IN USE, and give the PC back when its owner returns"""
    while True:
        try:
            idle = idle_seconds()
            available, reason = nimby_state(idle, owner_editor_open(), datetime.now())
            with _lock:
                NIMBY_STATUS.update(available=available, reason=reason,
                                    idle_minutes=None if idle is None else int(idle // 60))
                stage = CURRENT_STATUS["stage"]
                if stage == "IDLE" and not available:
                    CURRENT_STATUS["stage"] = "IN USE"
                elif stage == "IN USE" and available:
                    CURRENT_STATUS["stage"] = "IDLE"
                proc = _process
            rendering = stage in ("INITIALIZING", "RENDERING")
            if (rendering and NIMBY_ON_RETURN != "finish" and idle is not None
                    and idle < NIMBY_BACK_SECONDS and not _owner_back.is_set()):
                log("Workstation mode: the owner is back, stopping the render")
                _owner_back.set()
                if proc:
                    kill_process_tree(proc)
        except Exception as e:
            log(f"Workstation check failed: {e!r}")
        time.sleep(5)


def monitor_system_resources():
    """Sample CPU, GPU, RAM usage for the lifetime of the agent"""
    nvml = None
    gpu_handle = None
    try:
        import pynvml  # provided by the nvidia-ml-py package
        pynvml.nvmlInit()
        gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        nvml = pynvml
    except Exception as e:
        log(f"GPU monitoring unavailable: {e}")

    while True:
        try:
            cpu = round(psutil.cpu_percent(interval=1), 1)
            ram = round(psutil.virtual_memory().percent, 1)
            gpu = vram = None
            if nvml:
                try:
                    util = nvml.nvmlDeviceGetUtilizationRates(gpu_handle)
                    mem = nvml.nvmlDeviceGetMemoryInfo(gpu_handle)
                    gpu = util.gpu
                    vram = round(mem.used / mem.total * 100, 1)
                except Exception as e:
                    log(f"GPU sample failed: {e}")
            with _lock:
                METRICS.update(cpu_usage=cpu, ram_usage=ram, gpu_usage=gpu, vram_usage=vram)
        except Exception as e:
            log(f"Resource sample failed: {e}")
        time.sleep(1)


# ------------------------------
# JOB VALIDATION
# ------------------------------
def _is_under(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives
        return False


def validate_project_path(project):
    if not isinstance(project, str) or not project.strip():
        return "project is required"
    project = project.strip()
    if project.startswith("-"):
        return "project must be a path, not a command-line switch"
    if not project.lower().endswith(".uproject"):
        return "project must be a .uproject file"
    if not os.path.isabs(project):
        return "project must be an absolute path"
    if SWITCH_IN_PATH_RE.search(project):
        return "project path must not contain command-line switches (' -Name=')"
    normalized = os.path.normcase(os.path.abspath(project))
    if PROJECT_ROOTS:
        if not any(_is_under(normalized, root) for root in PROJECT_ROOTS):
            return "project is outside the allowed project roots (URF_PROJECT_ROOTS)"
    elif normalized.startswith("\\\\") or project.startswith(("\\\\", "//")):
        return "network (UNC) project paths require URF_PROJECT_ROOTS to be configured"
    if not os.path.isfile(project):
        return "project file not found on this node"
    return None


def validate_asset_path(value, field):
    if not isinstance(value, str) or not value.strip():
        return f"{field} is required"
    if len(value) > 512 or not ASSET_PATH_RE.match(value.strip()):
        return f"{field} must be an Unreal asset path like /Game/Folder/Asset"
    return None


def validate_job(data):
    """Return (job, error). job is a cleaned dict safe to put on the Unreal command line."""
    if not isinstance(data, dict):
        return None, "request body must be a JSON object"

    error = validate_project_path(data.get("project"))
    if error:
        return None, error
    kind = data.get("kind") or "render"
    if kind not in JOB_KINDS:
        return None, f"kind must be one of {', '.join(JOB_KINDS)}"
    if kind != "render" and UE_MODE != "executor":
        return None, "preparing a project needs URF_UE_MODE=executor on this node"
    if kind == "prepare-fill":
        data = {**data, "map": data.get("map") or "/Game/None", "config": data.get("config") or "/Game/None",
                "sequences": [WHOLE_PROJECT], "frame_start": None, "frame_end": None, "auto_split": None}
    for field in ("map", "config"):
        error = validate_asset_path(data.get(field), field)
        if error:
            return None, error
    shared_ddc = data.get("shared_ddc") or ""
    if shared_ddc and not (isinstance(shared_ddc, str) and len(shared_ddc) <= 260 and UNC_PATH_RE.match(shared_ddc)):
        return None, r"shared_ddc must be a network folder like \\192.168.1.20\share\FarmDDC"
    init_time = data.get("init_time") or ""
    if init_time and not (isinstance(init_time, str) and INIT_TIME_RE.match(init_time)):
        return None, "init_time must look like 2026-10-06 11:29:58"
    output_dir = data.get("output_dir") or ""
    if output_dir and not (isinstance(output_dir, str) and len(output_dir) <= 400
                           and OUTPUT_DIR_RE.match(output_dir)):
        return None, r"output_dir must be a folder like \\server\share\Renders or K:\Renders"
    if kind != "render" and (data.get("frame_start") is not None or data.get("auto_split")):
        return None, "a prepare job cannot have a frame range or sharing"

    sequences = data.get("sequences")
    if not isinstance(sequences, list) or not sequences:
        return None, "sequences must be a non-empty list"
    if len(sequences) > MAX_SEQUENCES:
        return None, f"at most {MAX_SEQUENCES} sequences per job"
    for seq in sequences:
        error = validate_asset_path(seq, "sequence")
        if error:
            return None, error

    job_id = data.get("job_id") or uuid.uuid4().hex[:12]
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        return None, "job_id may only contain letters, digits, '-' and '_'"

    frame_start, frame_end = data.get("frame_start"), data.get("frame_end")
    if (frame_start is None) != (frame_end is None):
        return None, "frame_start and frame_end must be given together"
    if frame_start is not None:
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (frame_start, frame_end)):
            return None, "frame_start and frame_end must be whole numbers"
        if not 0 <= frame_start <= frame_end <= MAX_FRAME:
            return None, f"frame range must satisfy 0 <= start <= end <= {MAX_FRAME}"
        if UE_MODE != "executor":
            return None, "frame ranges need URF_UE_MODE=executor on this node"
    auto = data.get("auto_split")
    if auto is not None:
        if not isinstance(auto, dict):
            return None, "auto_split must be an object"
        pieces, min_chunk, index = auto.get("pieces"), auto.get("min_chunk", 50), auto.get("index", 1)
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (pieces, min_chunk, index)):
            return None, "auto_split pieces, index and min_chunk must be whole numbers"
        if not (1 <= pieces <= 64 and 1 <= min_chunk <= 100000 and 1 <= index <= pieces):
            return None, "auto_split needs 1-64 pieces, a piece index within them and a minimum piece of 1-100000 frames"
        if frame_start is not None:
            return None, "auto_split and a frame range cannot be combined"
        if UE_MODE != "executor":
            return None, "automatic sharing needs URF_UE_MODE=executor on this node"
        auto = {"pieces": pieces, "index": index, "min_chunk": min_chunk}
    warmup = data.get("warmup", 0)
    if not isinstance(warmup, int) or isinstance(warmup, bool) or not 0 <= warmup <= 200:
        return None, "warmup must be a whole number from 0 to 200"

    return {
        "job_id": job_id,
        "project": data["project"].strip(),
        "map": data["map"].strip(),
        "config": data["config"].strip(),
        "sequences": [s.strip() for s in sequences],
        "frame_start": frame_start,
        "frame_end": frame_end,
        "warmup": warmup,
        "auto_split": auto,
        "kind": kind,
        "shared_ddc": shared_ddc,
        "output_dir": output_dir.strip(),
        "init_time": init_time,
        "fast_mode": data.get("fast_mode") is True,
    }, None


# ------------------------------
# RENDER EXECUTION
# ------------------------------
def scene_name(seq):
    return seq.rsplit("/", 1)[-1].split(".")[-1]


def build_command(job, seq):
    tail = ["-unattended", "-stdout", "-FullStdOutLogOutput"]
    if job.get("kind") == "prepare-fill":
        # Epic's cache fill: builds the Derived Data Cache for the whole project (can take hours)
        return [UE_EXE, job["project"], "-run=DerivedDataCache", "-fill"] + tail
    cmd = [
        UE_EXE,
        job["project"],
        job["map"],
        "-game",
        f"-LevelSequence={seq}",
        f"-MoviePipelineConfig={job['config']}",
    ]
    if SKIP_NDISPLAY:
        cmd.append(PLAIN_GAME_ENGINE)
    if job.get("fast_mode"):
        # Fast mode (Admin): no window to draw and no loading screen, which saves GPU time every frame
        cmd += ["-RenderOffscreen", "-NoLoadingScreen"]
    if UE_MODE == "executor":
        # Our Movie Render Queue executor (agent/unreal/urf_executor.py) renders the frame range
        # and reports progress/results; UE_PYTHONPATH (see ue_environment) makes Unreal load it.
        cmd += [
            "-MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelinePythonHostExecutor",
            "-ExecutorPythonClass=/Engine/PythonTypes.URFExecutor",
            f"-URFJob={job['job_id']}",
        ]
        status_file = progress_file_for(job)
        if status_file:
            cmd.append(f"-URFStatusFile={status_file}")
        if job.get("kind") == "prepare":
            cmd.append("-URFPrepare=1")
        elif job.get("frame_start") is not None:
            cmd += [f"-URFStart={job['frame_start']}", f"-URFEnd={job['frame_end']}"]
        elif job.get("auto_split"):
            cmd += [f"-URFAutoSplit={job['auto_split']['pieces']}",
                    f"-URFAutoIndex={job['auto_split'].get('index', 1)}",
                    f"-URFMinChunk={job['auto_split']['min_chunk']}"]
        if job.get("warmup"):
            cmd.append(f"-URFWarmup={job['warmup']}")
    return cmd + tail


def progress_file_for(job):
    """Where the executor writes its latest progress line (see follow_progress_file). Only a path
    without spaces, so it passes through Unreal's command line untouched."""
    path = os.path.join(LOG_DIR, f"{job['job_id']}.progress")
    return "" if " " in path or '"' in path else path


def read_result_file(path):
    """The executor's result, written next to the progress file (Unreal's stdout may hold it back)"""
    try:
        with open(path + ".result", encoding="utf-8") as f:
            tagged = parse_tagged_line(f.read().strip())
    except OSError:
        return None
    return tagged[1] if tagged and tagged[0] == "result" else None


def follow_progress_file(path, clock, stop):
    """Read the executor's progress file every 2 s: progress that does not wait for Unreal's buffered
    stdout. Counts as the heartbeat (and as output) for the freeze watchdog too."""
    last = None
    while not stop.wait(2):
        if not clock.get("result"):
            result = read_result_file(path)
            if result is not None:
                clock.update(result=True, result_at=clock.get("result_at") or time.time(), result_data=result)
        try:
            with open(path, encoding="utf-8") as f:
                line = f.read().strip()
            changed_at = os.path.getmtime(path)
        except OSError:
            continue
        if line and line != last:
            last = line
            if parse_ue_output(line):
                clock.update(progress=time.time(), rendering=True, completed_at=None)
        if line:
            # a rewrite (new progress or the 30 s heartbeat) shows Unreal is alive even when stdout is quiet
            clock["progress"] = max(clock["progress"], changed_at)
            clock["line"] = max(clock["line"], changed_at)


def shared_cache_for(job=None):
    return ((job or {}).get("shared_ddc") or SHARED_DDC).strip()


def ue_environment(job=None):
    """Environment for Unreal: in executor mode, put our Python scripts on UE_PYTHONPATH"""
    env = os.environ.copy()
    env.pop("URF_OUTPUT_DIR", None)
    env.pop("URF_INIT_TIME", None)
    if (job or {}).get("init_time"):
        env["URF_INIT_TIME"] = job["init_time"]  # same {date}/{time} on every computer of a shot
    if (job or {}).get("output_dir"):
        env["URF_OUTPUT_DIR"] = job["output_dir"]  # read by the executor inside Unreal (no quoting issues)
    cache = shared_cache_for(job)
    if cache:
        # Epic: this variable points the Shared DDC at a network folder, no project change needed
        env["UE-SharedDataCachePath"] = cache
    if UE_MODE == "executor":
        existing = env.get("UE_PYTHONPATH")
        env["UE_PYTHONPATH"] = UNREAL_SCRIPTS_DIR + (os.pathsep + existing if existing else "")
    return env


class ProcessJob:
    """A Windows Job Object holding Unreal and every process it starts (ShaderCompileWorker, crash
    reporter). Terminating the job kills them all at once, including ones started a moment ago; closing
    it (agent exit too) kills what is left. Elsewhere, or if Windows refuses, falls back to psutil."""

    def __init__(self, proc):
        self.handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not (k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                    and k32.AssignProcessToJobObject(handle, int(proc._handle))):
                error = ctypes.get_last_error()
                k32.CloseHandle(handle)
                raise OSError(error, "could not put Unreal in a job object")
            self.handle, self._k32 = handle, k32
        except Exception as e:  # never stop a render over this; psutil still kills the tree
            log(f"Job object unavailable, falling back to process-tree kill: {e}")

    def terminate(self):
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self._k32.CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE: nothing it started survives
            self.handle = None


def stall_reason(now, last_line_at, last_progress_at, rendering,
                 load_minutes=None, render_minutes=None):
    """Why Unreal looks frozen (a message), or None while it is still working"""
    load_minutes = LOAD_STALL_MINUTES if load_minutes is None else load_minutes
    render_minutes = RENDER_STALL_MINUTES if render_minutes is None else render_minutes
    if rendering and render_minutes > 0 and now - last_progress_at > render_minutes * 60:
        return (f"Unreal froze while rendering (its engine stopped for {render_minutes:g} minutes) "
                "and was stopped. Known causes: Niagara Audio Spectrum, GPU particles with EXR/PNG output.")
    if load_minutes > 0 and now - last_line_at > load_minutes * 60:
        return f"Unreal stopped responding (no output for {load_minutes:g} minutes) and was stopped."
    return None


def script_trouble(clock, now):
    """Why the farm's script inside Unreal can no longer finish the render (a message), or None"""
    if clock.get("script_errors", 0) >= SCRIPT_ERROR_LIMIT:
        return (f"The farm's script inside Unreal kept failing ({clock['script_errors']} errors), so Unreal was "
                f"stopped. Last error: {clock.get('script_error') or 'see the render log'}")
    done = clock.get("completed_at")
    if done and not clock.get("result") and now - done > NO_RESULT_AFTER_DONE_SECONDS:
        return ("Unreal finished the render but the farm's script did not report back, so Unreal was stopped. "
                "The frames are probably on the drive; they were not checked. "
                f"Last script error: {clock.get('script_error') or 'none'}")
    return None


def watch_for_freeze(proc, clock, stop, load_minutes=None):
    """Kill Unreal when it stops making progress; returns via clock["frozen"] the reason.
    Never dies on an error: a watchdog that stopped silently would let a hung render sit forever."""
    while not stop.wait(15):
        try:
            if proc.poll() is not None:
                return
            now = time.time()
            if clock.get("result"):
                if now - clock.get("result_at", now) > RESULT_GRACE_SECONDS and not clock.get("closed"):
                    clock["closed"] = True
                    log(f"Watchdog: Unreal did not close {RESULT_GRACE_SECONDS} s after reporting; stopping it")
                    kill_process_tree(proc)
                continue  # the render is done; nothing below may turn it into a failure
            reason = script_trouble(clock, now) or stall_reason(
                now, clock["line"], clock["progress"], clock["rendering"], load_minutes=load_minutes)
            if reason and not clock["frozen"]:
                clock["frozen"] = reason
                log(f"Watchdog: {reason}")
            if clock["frozen"]:
                kill_process_tree(proc)  # repeated every pass until Unreal is really gone
        except Exception as e:
            log(f"Watchdog error (still watching): {e!r}")


def kill_process_tree(proc):
    """Kill only the Unreal process this agent started, plus its children.
    The job object (when there is one) kills them all at once; the psutil sweep covers the rest."""
    job = getattr(proc, "farm_job", None)
    if job:
        try:
            job.terminate()
        except Exception as e:
            log(f"Could not terminate the job object: {e!r}")
    for _ in range(2):  # a second pass catches a child started while the first was killing
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        for p in children + [parent]:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


# Lines that say WHY Unreal failed, as opposed to the shutdown noise that follows a crash
CRITICAL_LINE_RE = re.compile(
    r"Assertion failed|Fatal error|: Fatal:|Array index out of bounds|Unhandled Exception|Failed to find|"
    r"Serial size mismatch|Crash in runnable thread|Ensure condition failed|Out of video memory|"
    r"D3D12.*(lost|removed)|LogMovieRenderPipeline: Error:|LogMovieRenderPipeline: Fatal", re.IGNORECASE)
# Windows reported a broken read from a network drive (seen 2026-10-05 on N: during a load)
NETWORK_ERROR_RE = re.compile(r"LastError=(59|64|121|1231)\b|unexpected network error|network name is no longer available",
                              re.IGNORECASE)
NETWORK_NOTE = ("Lost the network connection to the project drive while Unreal was loading "
                "(Windows reported a network read error), so Unreal read a broken file and closed. "
                "The farm retries automatically; if it keeps happening, check the file server and the network.")
NOISE_LINE_RE = re.compile(r"OpenXR|Failed to load '[^']+\.dll'")
LOG_PREFIX_RE = re.compile(r"^\[[^\]]*\]\[\s*\d+\]")


def _clean_error(line):
    text = LOG_PREFIX_RE.sub("", line).strip()
    for prefix in ("LogWindows: Error:", "appError called:", "LogOutputDevice: Error:", "LogThreadingWindows: Error:",
                   "LogLinker: Fatal:", "LogMovieRenderPipeline: Error:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = re.sub(r"\[AssetLog\]\s*", "", text)
    return re.sub(r"\s*\[File:[^\]]*\]\s*\[Line:\s*\d+\]", "", text).strip()


def summarize_errors(lines, limit=3):
    """The few distinct log lines that explain a failure, without timestamps or repeats.

    `lines` may include the line after each critical one: Unreal often prints the message there
    ("Assertion failed:" + next line "...: Serial size mismatch ...")."""
    found = []
    for i, line in enumerate(lines):
        if (not CRITICAL_LINE_RE.search(line) or "[Callstack]" in line
                or "Warning:" in line or NOISE_LINE_RE.search(line)):
            continue
        text = _clean_error(line)
        if text.endswith(":") and i + 1 < len(lines) and not LOG_PREFIX_RE.match(lines[i + 1]):
            text = f"{text} {lines[i + 1].strip()}"  # the message continues on the next line
        if len(text) < 16 or text.endswith(":"):
            continue  # a bare "Fatal error:" heading; the next line says what
        if text not in found and not any(text in f or f in text for f in found):
            found.append(text[:300])
        if len(found) >= limit:
            break
    return found


def record_result(job, seq, status, started, exit_code=None, detail="", log_file="", frames=None):
    global _result_seq
    with _lock:
        _result_seq += 1
        result = {
            "seq": _result_seq,
            "job_id": job["job_id"],
            "project": job["project"],
            "sequence": seq,
            "scene": scene_name(seq),
            "frame_start": job.get("frame_start"),
            "frame_end": job.get("frame_end"),
            "status": status,
            "exit_code": exit_code,
            "frames": frames if frames is not None else CURRENT_STATUS["current_frame"],
            "duration": format_duration(time.time() - started),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "detail": detail[:1000],
            "log_file": log_file,
            "discovered": job.get("discovered"),
            # a dropped network drive, not a problem with the render: the master retries without counting it
            "network_error": status == "FAILED" and NETWORK_NOTE in detail,
            "out_of_memory": status == "FAILED" and bool(OUT_OF_MEMORY_RE.search(detail)),
            "owner_returned": status == "FAILED" and detail.startswith(OWNER_NOTE),
        }
        _results.append(result)
        CURRENT_STATUS["last_result"] = result
    log(f"[{result['finished_at']}] {seq}: {status} {detail}")
    return result


def open_render_log(job, seq):
    """Open a log file for this render's Unreal output and prune old ones. Never fatal."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(LOG_DIR, f"{stamp}_{job['job_id']}_{scene_name(seq)}.log")
        handle = open(path, "w", encoding="utf-8", errors="replace")
        old = sorted(f for f in os.listdir(LOG_DIR) if f.endswith(".log"))
        for name in old[:-LOGS_KEPT]:
            try:
                os.remove(os.path.join(LOG_DIR, name))
            except OSError:
                pass
        return handle, path
    except OSError as e:
        log(f"Could not write render log in {LOG_DIR}: {e}")
        return None, ""


def run_sequence(job, seq):
    """Render one sequence and return its final status (COMPLETED / FAILED / CANCELLED)"""
    global _process

    started = time.time()
    update_status(
        sequence=seq,
        scene=scene_name(seq),
        stage="INITIALIZING",
        progress=0,
        current_frame=0,
        total_frames=0,
        fps=0,
        eta="",
        start_time=None,
        discovered=None,
        first_frame=None,
        activity="Opening Unreal and loading the project"
    )

    try:
        proc = subprocess.Popen(
            build_command(job, seq),
            env=ue_environment(job),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",  # an undecodable log byte must never kill the reader
            bufsize=1
        )
    except OSError as e:
        return record_result(job, seq, "FAILED", started, detail=f"Could not start Unreal: {e}")["status"]

    proc.farm_job = ProcessJob(proc)
    with _lock:
        _process = proc
        if _cancel.is_set() or _owner_back.is_set():
            kill_process_tree(proc)
        else:
            CURRENT_STATUS["stage"] = "RENDERING"
            CURRENT_STATUS["start_time"] = time.time()

    tail = collections.deque(maxlen=OUTPUT_TAIL_LINES)
    critical = []  # lines that explain a failure (see summarize_errors)
    critical_follow = False
    network_error = False
    log_handle, log_file = open_render_log(job, seq)
    progress_lines = 0
    executor_result = None
    # "rendering" turns on with the executor's first URF_PROGRESS line; its heartbeats keep "progress"
    # fresh while the engine ticks. Legacy mode has no heartbeat, so only the no-output rule applies.
    clock = {"line": time.time(), "progress": time.time(), "rendering": False, "frozen": None}
    stop_watch = threading.Event()
    load_minutes = FILL_STALL_MINUTES if job.get("kind") == "prepare-fill" else None
    threading.Thread(target=watch_for_freeze, args=(proc, clock, stop_watch, load_minutes), daemon=True).start()
    status_file = progress_file_for(job) if UE_MODE == "executor" else ""
    reader = None
    if status_file:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            for stale in (status_file, status_file + ".result"):  # never read an earlier run's files
                if os.path.exists(stale):
                    os.remove(stale)
        except OSError:
            pass
        reader = threading.Thread(target=follow_progress_file, args=(status_file, clock, stop_watch), daemon=True)
        reader.start()
    try:
        for line in proc.stdout:
            clock["line"] = time.time()
            if log_handle:
                log_handle.write(line)
            if parse_ue_output(line):
                progress_lines += 1
            tagged = parse_tagged_line(line)
            if tagged and tagged[0] == "progress":
                clock.update(progress=time.time(), rendering=True, completed_at=None)
            if tagged and tagged[0] == "result":
                clock.update(result=True, result_at=clock.get("result_at") or time.time())
            if "LogScript: Error: Script Msg" in line:
                clock["script_errors"] = clock.get("script_errors", 0) + 1
            error_text = SCRIPT_ERROR_RE.match(line)
            if error_text:
                clock["script_error"] = f"{error_text.group(1)}: {error_text.group(2).strip()}"[:300]
            if "Movie Pipeline completed" in line:
                clock["completed_at"] = time.time()
            if tagged and tagged[0] == "result":
                executor_result = tagged[1]
            if tagged and tagged[0] == "range":
                # Automatic sharing: publish the plan now (the master polls /status) so other
                # computers start their pieces while this one renders the first
                discovered = {**tagged[1], "job_id": job["job_id"]}
                update_status(discovered=discovered)
                pieces = discovered.get("ranges") or []
                index = int(discovered.get("index") or 1)
                job = {**job, "discovered": discovered}
                if len(pieces) > 1 and index <= len(pieces) and job.get("kind", "render") == "render":
                    job.update(frame_start=pieces[index - 1][0], frame_end=pieces[index - 1][1])
            if not tagged and line.strip():
                tail.append(line.rstrip())  # Unreal's own lines, for the failure summary
                if len(critical) < 60 and (CRITICAL_LINE_RE.search(line) or critical_follow):
                    critical.append(line.rstrip())
                critical_follow = bool(CRITICAL_LINE_RE.search(line))  # keep the next line too
                if NETWORK_ERROR_RE.search(line):
                    network_error = True
    except Exception:
        kill_process_tree(proc)  # never leave an unmonitored Unreal running
        raise
    finally:
        stop_watch.set()
        proc.stdout.close()
        exit_code = proc.wait()
        proc.farm_job.close()  # kills anything Unreal left behind (e.g. a ShaderCompileWorker)
        if reader:
            reader.join(5)  # a line it is still parsing must not land in the next render's status
        if executor_result is None and status_file:
            executor_result = read_result_file(status_file)
        if job.get("kind") == "prepare":
            # the throw-away frames of "Prepare project" (Unreal may have been stopped before it cleaned up)
            shutil.rmtree(os.path.join(tempfile.gettempdir(), "URF_prepare", job["job_id"]), ignore_errors=True)
        if status_file:
            for leftover in (status_file, status_file + ".tmp", status_file + ".result", status_file + ".result.tmp"):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        if log_handle:
            log_handle.close()
        with _lock:
            _process = None

    if _owner_back.is_set():
        return record_result(job, seq, "FAILED", started, exit_code, OWNER_NOTE, log_file)["status"]
    if _cancel.is_set():
        return record_result(job, seq, "CANCELLED", started, exit_code, log_file=log_file)["status"]
    last_lines = " | ".join(line for line in list(tail)[-5:] if line)
    reasons = summarize_errors(critical)
    if network_error:
        reasons = [NETWORK_NOTE] + reasons[:2]
    last_note = (f" Unreal said: {' | '.join(reasons)}" if reasons
                 else f" Last output: {last_lines}" if last_lines else "")
    if clock["frozen"] and executor_result is None:  # a reported result always wins over a late freeze
        return record_result(job, seq, "FAILED", started, exit_code, log_file=log_file,
                             detail=clock["frozen"] + last_note)["status"]
    if job.get("kind") == "prepare-fill":
        if exit_code == 0:
            return record_result(job, seq, "COMPLETED", started, exit_code,
                                 "Whole project prepared: the cache is filled", log_file)["status"]
        return record_result(job, seq, "FAILED", started, exit_code, log_file=log_file,
                             detail=f"Preparing the project failed (exit code {exit_code}).{last_note}")["status"]

    if UE_MODE == "executor":
        # The executor's own report decides; Unreal's exit code alone can be 0 after a failed render
        if executor_result is None:
            if reasons:  # Unreal stopped before our executor reported, and said why
                detail = f"Unreal stopped (exit code {exit_code}). Unreal said: {' | '.join(reasons)}"
            else:
                detail = (f"Unreal exited with code {exit_code} without a result from the URF executor. "
                          "Is the 'Python Editor Script Plugin' enabled in the project?" + last_note)
            return record_result(job, seq, "FAILED", started, exit_code, log_file=log_file,
                                 detail=detail)["status"]
        if not executor_result.get("success"):
            return record_result(
                job, seq, "FAILED", started, exit_code, log_file=log_file,
                detail=f"Render failed: {executor_result.get('error') or 'unknown error'}.{last_note}"
            )["status"]
        files = executor_result.get("files_per_pass") or {}
        bad = executor_result.get("bad_files") or {}
        if bad.get("count"):
            frames = ", ".join(str(f) for f in bad.get("frames") or [])
            return record_result(
                job, seq, "FAILED", started, exit_code, log_file=log_file,
                detail=(f"{bad['count']} frame file(s) missing or empty on the drive"
                        f"{': frames ' + frames if frames else ''}. The output drive may have dropped or be full; "
                        "the farm renders these frames again.")
            )["status"]
        notes = [executor_result["note"]] if executor_result.get("note") else []
        nothing_to_do = str(executor_result.get("note") or "").startswith("Nothing to render")
        if job.get("kind", "render") == "render" and not any(files.values()) and not nothing_to_do:
            return record_result(
                job, seq, "FAILED", started, exit_code, log_file=log_file,
                detail=("Movie Render Queue reported success but wrote no files. Check the preset's outputs "
                        "(at least one image or video output switched on) and the frame range.")
            )["status"]
        if executor_result.get("frame_count_mismatch"):
            notes.append(f"Wrote {files} files per pass but the task covers "
                         f"{executor_result.get('expected_frames')} frames: check the frame-range setting")
        speed = seconds_per_frame()
        if speed and speed >= 0.05:  # a real frame; instant test renders say nothing
            notes.append(f"{speed:.1f} s per frame")
        if clock.get("closed"):
            notes.append("Unreal did not close by itself after the render and was stopped")
        elif exit_code != 0:
            notes.append(f"Unreal exited with code {exit_code} after reporting success")
        return record_result(job, seq, "COMPLETED", started, exit_code, " ".join(notes), log_file,
                             frames=max(files.values(), default=None))["status"]

    if exit_code == 0:
        detail = "" if progress_lines else (
            "Finished, but no frame-progress lines were recognized. "
            f"Run tools/check_ue_log.py on {log_file or 'the render log'} so the parser can be fixed."
        )
        return record_result(job, seq, "COMPLETED", started, exit_code, detail, log_file)["status"]
    return record_result(
        job, seq, "FAILED", started, exit_code,
        detail=f"Unreal exited with code {exit_code}.{last_note}", log_file=log_file
    )["status"]


def run_job(job):
    try:
        for seq in job["sequences"]:
            if _cancel.is_set() or _owner_back.is_set():
                break
            try:
                status = run_sequence(job, seq)
            except Exception as e:
                status = record_result(job, seq, "FAILED", time.time(), detail=f"Agent error: {e}")["status"]
            if status == "CANCELLED":
                break
    finally:
        update_status(
            job_id="",
            project="",
            sequence="",
            scene="",
            discovered=None,   # the master must not re-read an old split plan from an idle computer
            first_frame=None,
            activity="",
            stage="IDLE",
            progress=0,
            current_frame=0,
            total_frames=0,
            fps=0,
            eta="",
            start_time=None
        )


# ------------------------------
# SELF-REGISTRATION WITH THE MASTER
# ------------------------------
def local_ip_towards(url):
    """The LAN address this machine uses to reach the master (no packets are sent)"""
    parsed = urlparse(url)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((parsed.hostname, parsed.port or 80))
        return s.getsockname()[0]


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """The farm token must only ever go to the configured master, never to where it redirects"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECTS = urllib.request.build_opener(_RefuseRedirect)


def master_url_problem(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return f"URF_MASTER_URL must look like http://192.168.1.5:5000 (got {url!r})"
    return None


def register_with_master():
    """Announce this node to the master. Returns the master's reply, e.g. {"status": "registered"}."""
    body = json.dumps({"name": NODE_NAME, "ip": local_ip_towards(MASTER_URL)}).encode()
    req = urllib.request.Request(
        MASTER_URL + "/register-node", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Farm-Token": FARM_TOKEN})
    try:
        with _NO_REDIRECTS.open(req, timeout=5) as resp:
            reply = json.load(resp)
            return reply if isinstance(reply, dict) else {"status": "error", "error": "unexpected reply"}
    except urllib.error.HTTPError as e:
        try:
            reason = json.load(e).get("error", e.reason)
        except ValueError:
            reason = e.reason
        return {"status": "error", "error": f"HTTP {e.code}: {reason}"}


def registration_loop():
    """Re-register periodically so a changed IP (DHCP) or a master restart is picked up"""
    problem = master_url_problem(MASTER_URL)
    if problem:
        log(f"Registration off: {problem}")
        return
    last_message = None
    while True:
        try:
            result = register_with_master()
            message = result.get("error") or f"master says: {result.get('status')}"
            if result.get("reachable") is False:
                message += f" - but the master cannot reach this node on port {PORT} (check this PC's firewall)"
        except (OSError, ValueError) as e:
            message = f"master {MASTER_URL} unreachable: {e}"
        except Exception as e:  # keep trying: a dead registration thread would never re-register
            message = f"registration error: {e!r}"
        if message != last_message and message != "master says: unchanged":
            log(f"Registration: {message}")
        last_message = message
        time.sleep(REGISTER_INTERVAL)


# ------------------------------
# AUTH
# ------------------------------
def require_token(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not FARM_TOKEN:
            return jsonify({"error": "agent has no URF_FARM_TOKEN configured"}), 503
        supplied = request.headers.get("X-Farm-Token", "")
        if not hmac.compare_digest(supplied.encode(), FARM_TOKEN.encode()):
            return jsonify({"error": "unauthorized"}), 401
        return view(*args, **kwargs)
    return wrapper


# ------------------------------
# API ROUTES
# ------------------------------
@app.route('/render', methods=['POST'])
@require_token
def render():
    job, error = validate_job(request.get_json(silent=True))
    if error:
        return jsonify({"error": error, "node": NODE_NAME}), 400

    with _lock:
        if CURRENT_STATUS["stage"] != "IDLE":
            return jsonify({"error": "node is busy", "node": NODE_NAME}), 409
        _cancel.clear()
        _owner_back.clear()
        CURRENT_STATUS.update(job_id=job["job_id"], project=job["project"], stage="INITIALIZING")

    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify({"status": "started", "node": NODE_NAME, "job_id": job["job_id"]})


def check_cache_folder(path):
    """Can this computer (as the user Unreal runs as) create files in the shared cache folder?"""
    if not (isinstance(path, str) and len(path) <= 260 and UNC_PATH_RE.match(path)):
        return {"ok": False, "error": r"not a network folder like \\server\share\FarmDDC"}
    test = os.path.join(path, f".farm-check-{NODE_NAME}-{uuid.uuid4().hex[:6]}")
    try:
        os.makedirs(path, exist_ok=True)
        with open(test, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test)
        return {"ok": True, "error": ""}
    except OSError as e:
        return {"ok": False, "error": f"{e.strerror or e} (Windows user {os.environ.get('USERNAME', '?')})"}


@app.route('/check-cache', methods=['POST'])
@require_token
def check_cache():
    path = (request.get_json(silent=True) or {}).get("path") or SHARED_DDC
    return jsonify({"node": NODE_NAME, **check_cache_folder(path)})


@app.route('/cancel', methods=['POST'])
@require_token
def cancel():
    log(f"CANCEL REQUEST RECEIVED ON: {NODE_NAME}")
    with _lock:
        if CURRENT_STATUS["stage"] == "IDLE":
            return jsonify({"status": "idle", "node": NODE_NAME})
        _cancel.set()
        CURRENT_STATUS["stage"] = "CANCELLING"
        proc = _process

    # The render thread records the CANCELLED result and resets to IDLE once Unreal exits
    if proc:
        kill_process_tree(proc)
    return jsonify({"status": "cancelling", "node": NODE_NAME})


@app.route('/status')
@require_token
def status():
    """Return current node status with all metrics"""
    with _lock:
        snapshot = {k: v for k, v in CURRENT_STATUS.items() if k != "start_time"}
        snapshot.update(METRICS)
        snapshot["nimby"] = dict(NIMBY_STATUS)
        snapshot.update(node=NODE_NAME, boot_id=BOOT_ID, result_seq=_result_seq,
                        agent_version=AGENT_VERSION, features=FEATURES,
                        elapsed=int(time.time() - CURRENT_STATUS["start_time"]) if CURRENT_STATUS["start_time"] else 0)
    return jsonify(snapshot)


@app.route('/results')
@require_token
def results():
    """Finished sequence results with seq greater than ?since=N"""
    since = request.args.get("since", default=0, type=int)
    with _lock:
        items = [r for r in _results if r["seq"] > since]
    return jsonify({"node": NODE_NAME, "boot_id": BOOT_ID, "results": items})


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "online",
        "node": NODE_NAME,
        "timestamp": datetime.now().isoformat()
    })


def setup_logging():
    # Consoles/redirected logs use the ANSI codepage, which can't encode every character Unreal prints
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
    if len(FARM_TOKEN) < MIN_TOKEN_LENGTH or FARM_TOKEN.lower().startswith("change-me"):
        raise SystemExit(
            f"Set URF_FARM_TOKEN to a shared secret of at least {MIN_TOKEN_LENGTH} characters "
            "(the same value on the master and every agent)."
        )
    if not os.path.isfile(UE_EXE):
        log(f"WARNING: Unreal not found at {UE_EXE} (set URF_UE_EXE)")
    if not PROJECT_ROOTS:
        log("WARNING: URF_PROJECT_ROOTS not set; any local .uproject path will be accepted")

    threading.Thread(target=monitor_system_resources, daemon=True).start()
    if NIMBY:
        log(f"Workstation mode: renders after {NIMBY_IDLE_MINUTES:g} min without keyboard or mouse"
            + (f", only {NIMBY_HOURS}" if NIMBY_HOURS else ""))
        threading.Thread(target=nimby_loop, daemon=True).start()
    if MASTER_URL:
        threading.Thread(target=registration_loop, daemon=True).start()
    else:
        log("URF_MASTER_URL not set; register this node in the dashboard by hand")

    log(f"🚀 Render Agent v2.0 starting on {NODE_NAME}")
    log(f"📡 Listening on {BIND_HOST}:{PORT}...")
    try:
        from waitress import serve
        serve(app, host=BIND_HOST, port=PORT, threads=8)
    except ImportError:
        app.run(host=BIND_HOST, port=PORT, threaded=True)
