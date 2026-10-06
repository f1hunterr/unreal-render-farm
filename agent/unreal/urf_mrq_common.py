"""Pure helpers shared by the Unreal-side executor and the agent. No `unreal` import, so it is testable."""
import json
import re
import time

VIDEO_OUTPUT_CLASSES = ("MoviePipelineAppleProResOutput", "MoviePipelineAvidDNxOutput",
                        "MoviePipelineCommandLineEncoder", "MoviePipelineWaveOutput",
                        "MoviePipelineMP4EncoderOutput")  # MP4: Unreal 5.6+


def is_image_output(class_name):
    """Image-sequence outputs (JPG, PNG, EXR, BMP, ...): one file per frame, so they split cleanly"""
    return "ImageSequenceOutput" in class_name


def drive_kind(path, get_drive_type=None):
    """'network' for UNC paths and mapped network drives, 'local' for a drive only this
    computer has (fixed, removable, RAM), else 'unknown'."""
    text = str(path or "").strip().strip('"')
    if text.startswith(("\\\\", "//")):
        return "network"
    if len(text) < 2 or text[1] != ":" or not text[0].isalpha():
        return "unknown"
    if get_drive_type is None:
        try:
            import ctypes
            get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        except (AttributeError, OSError):
            return "unknown"
    kind = get_drive_type(text[0].upper() + ":\\")
    return {4: "network", 2: "local", 3: "local", 6: "local"}.get(kind, "unknown")


def choose_output_dir(farm_dir, preset_dir, project_dir, kind_of=drive_kind):
    """Where the frames go: (folder to use instead of the preset's, or "" to keep it; a note).

    1. The farm's folder (Admin setting or the render's own "Save frames to") always wins.
    2. A preset that saves to a drive only the rendering computer has (e.g. E:) would scatter the
       frames over the farm's computers: they go next to the project instead, which is shared."""
    if farm_dir:
        return farm_dir, f"Frames saved to {farm_dir}"
    project_dir = str(project_dir or "").rstrip("\\/")
    resolved = str(preset_dir or "").replace("{project_dir}", project_dir + "/")
    if not resolved or not project_dir:
        return "", ""
    if kind_of(resolved) == "local" and kind_of(project_dir) == "network":
        target = project_dir + "/Renders/{sequence_name}"
        return target, (f"The preset saves to {preset_dir}, a drive only the rendering computer has, so the "
                        f"frames were saved on the shared drive instead: {target}")
    return "", ""


PROGRESS_TAG = "URF_PROGRESS"
RESULT_TAG = "URF_RESULT"
RANGE_TAG = "URF_RANGE"   # automatic splitting: the shot's frames and how they were cut

# Movie Render Queue's custom playback range treats the end frame as exclusive (like Sequencer's
# playback range). Our tasks are inclusive [start, end], so we pass end + 1.
# VERIFY ON THE FIRST REAL RENDER: a task 101-200 must write exactly 100 frames per pass.
MRQ_END_EXCLUSIVE = True


def task_from_params(params):
    """Read our task settings from Unreal's parsed command-line parameters.

    Returns {"start": int|None, "end": int|None, "warmup": int, "job_id": str}.
    """
    def as_int(name):
        value = params.get(name)
        if value in (None, ""):
            return None
        return int(str(value).strip('"'))

    start, end = as_int("URFStart"), as_int("URFEnd")
    if (start is None) != (end is None):
        raise ValueError("URFStart and URFEnd must be given together")
    if start is not None and end < start:
        raise ValueError(f"URFEnd ({end}) is before URFStart ({start})")
    pieces = as_int("URFAutoSplit") or 0
    if pieces and start is not None:
        raise ValueError("URFAutoSplit cannot be combined with URFStart/URFEnd")
    return {
        "start": start,
        "end": end,
        "warmup": max(0, as_int("URFWarmup") or 0),
        "job_id": str(params.get("URFJob", "")).strip('"'),
        "auto_pieces": max(0, pieces),
        "auto_index": max(1, as_int("URFAutoIndex") or 1),
        "min_chunk": max(1, as_int("URFMinChunk") or DEFAULT_MIN_CHUNK),
        "prepare": bool(as_int("URFPrepare")),
    }


DEFAULT_MIN_CHUNK = 50  # frames: smaller pieces spend more time starting Unreal than rendering


CUT_SNAP = 0.25  # a piece boundary moves to a camera cut up to this share of a piece away


def plan_auto_split(start, end, pieces, min_chunk=DEFAULT_MIN_CHUNK, cuts=()):
    """Cut the inclusive range [start, end] into at most `pieces` contiguous ranges,
    none shorter than min_chunk (except a short shot, which stays whole).

    cuts: first frames of the sequence's camera cuts / shots. Epic: the smallest unit of work that
    splits cleanly is a camera cut (motion blur, TSR and particles need the previous frame), so each
    boundary moves to the nearest cut when one is close. Every computer plans the same shot on its
    own, so this must stay deterministic."""
    total = end - start + 1
    if total <= 0:
        raise ValueError(f"empty frame range {start}-{end}")
    min_chunk = max(1, min_chunk)
    pieces = max(1, min(pieces, total // min_chunk or 1))
    # Even pieces (sizes differ by at most one frame), so none is shorter than total // pieces >= min_chunk
    base, extra = divmod(total, pieces)
    bounds, at = [], start  # first frame of every piece after the first
    for i in range(pieces - 1):
        at += base + (1 if i < extra else 0)
        bounds.append(at)
    usable = sorted({int(c) for c in cuts if start < int(c) <= end})
    reach = int(base * CUT_SNAP)
    if usable and reach and pieces > 1:
        snapped, prev = [], start
        for i, b in enumerate(bounds):
            following = bounds[i + 1] if i + 1 < len(bounds) else end + 1
            near = min(usable, key=lambda c: (abs(c - b), c))
            # move onto the cut only if both pieces it touches stay at least min_chunk long
            if abs(near - b) <= reach and near - prev >= min_chunk and following - near >= min_chunk:
                b = near
            snapped.append(b)
            prev = b
        bounds = snapped
    starts = [start] + bounds
    return [(s, (starts[i + 1] - 1) if i + 1 < len(starts) else end) for i, s in enumerate(starts)]


def cuts_used(ranges, cuts):
    """How many piece boundaries fall exactly on a camera cut"""
    cut_set = {int(c) for c in cuts}
    return sum(1 for a, _ in ranges[1:] if a in cut_set)


PREPARE_LIMIT = 100  # frames per prepare run (each costs a few seconds plus warm-up)


def prepare_frames(start, end, cuts=(), limit=PREPARE_LIMIT):
    """Prepare (warm the cache): one frame at the start of each camera cut, so every shot's
    shaders and textures are built once without rendering the whole sequence.
    Returns (frames to render, number of cuts found): the caller says when some were left out."""
    frames = sorted({start} | {int(c) for c in cuts if start <= int(c) <= end})
    return frames[:limit] if limit else frames, len(frames)


def frame_number(path):
    """Frame number in an output file name like Shot_010.0105.exr -> 105, else None"""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    match = re.search(r"(\d+)(?=\.[^.]+$)", name)
    return int(match.group(1)) if match else None


def file_ok(path, size_of):
    try:
        return size_of(path) > 0
    except OSError:
        return False


def still_bad_files(paths, size_of, tries=3, wait=2.0, sleep=time.sleep):
    """Written files that are missing or empty, looked at again a few times: a NAS can show a file
    late or at 0 bytes for a moment after Unreal wrote it (write-behind caching)."""
    bad = [p for p in paths if not file_ok(p, size_of)]
    for _ in range(tries):
        if not bad:
            break
        sleep(wait)
        bad = [p for p in bad if not file_ok(p, size_of)]
    return bad


def bad_files_report(paths, size_of):
    """Output files that are missing or empty. size_of(path) returns bytes or raises OSError."""
    bad = []
    for path in paths:
        try:
            if size_of(path) > 0:
                continue
        except OSError:
            pass
        bad.append(path)
    frames = sorted({f for f in (frame_number(p) for p in bad) if f is not None})
    return {"count": len(bad), "frames": frames[:20], "examples": bad[:3]}


def range_line(job_id, start, end, ranges, note="", index=1, cuts=0):
    """The shot's frames, the planned pieces, and which piece (1-based) this computer renders.
    cuts: how many piece boundaries were moved onto camera cuts."""
    return f"{RANGE_TAG} " + json.dumps({"job_id": job_id, "start": start, "end": end, "index": index,
                                        "ranges": [list(r) for r in ranges], "note": note, "cuts": cuts})


def mrq_range(start, end):
    """Inclusive task range -> (custom_start_frame, custom_end_frame) for Movie Render Queue"""
    return start, end + 1 if MRQ_END_EXCLUSIVE else end


def expected_frames(start, end):
    return None if start is None else end - start + 1


def progress_line(percent, current=None, total=None):
    return f"{PROGRESS_TAG} " + json.dumps({"percent": round(float(percent), 1), "current": current, "total": total})


def result_line(success, files_per_pass, start=None, end=None, error="", note="", bad_files=None):
    """Final report. files_per_pass: {pass name: number of files written}.
    bad_files: bad_files_report() of the written files (missing or 0 bytes on disk)."""
    expected = expected_frames(start, end)
    counts = list(files_per_pass.values())
    mismatch = bool(expected is not None and counts and any(c != expected for c in counts))
    return f"{RESULT_TAG} " + json.dumps({
        "success": bool(success),
        "files_per_pass": files_per_pass,
        "expected_frames": expected,
        "frame_count_mismatch": mismatch,
        "error": error,
        "note": note,
        "bad_files": bad_files or {"count": 0, "frames": [], "examples": []},
    })


def parse_tagged_line(line):
    """('progress'|'result', dict) for a line our executor printed (possibly behind a log prefix), else None"""
    for kind, tag in (("progress", PROGRESS_TAG), ("result", RESULT_TAG), ("range", RANGE_TAG)):
        index = line.find(tag + " {")
        if index != -1:
            try:
                return kind, json.loads(line[index + len(tag) + 1:].strip())
            except ValueError:
                return None
    return None
