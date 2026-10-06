"""Check what the render agent's parser understands in a real Unreal render log.

Usage (on a render node, from the project folder):
    python tools/check_ue_log.py C:\\UnrealRenderFarm\\agent\\logs\\<render>.log

Prints the progress the agent would show, and the lines that look like progress
but were NOT recognized. To lock a log in as a regression test, copy it to
tests/fixtures/ue_logs/<name>.log and write <name>.json next to it, e.g.
    {"current_frame": 120, "total_frames": 120, "source": "UE 5.6 MRQ, PNG sequence"}
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import farm_agent as agent  # noqa: E402
agent.UE_MODE = "legacy"  # read Unreal's own progress lines as well as the executor's

LOOKS_LIKE_PROGRESS = re.compile(r"frame|%|MoviePipeline|MovieRender|Rendering", re.IGNORECASE)


def check(path):
    agent.update_status(stage="RENDERING", current_frame=0, total_frames=0, progress=0, start_time=None)
    matched, unmatched = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f, 1):
            if agent.parse_ue_output(line):
                matched.append((number, line.rstrip()))
            elif LOOKS_LIKE_PROGRESS.search(line):
                unmatched.append((number, line.rstrip()))
    return matched, unmatched, dict(agent.CURRENT_STATUS)


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    matched, unmatched, status = check(sys.argv[1])

    print(f"Recognized progress lines: {len(matched)}")
    for number, line in (matched if len(matched) <= 6 else matched[:3] + matched[-3:]):
        print(f"  {number:>6}: {line[:160]}")
    print(f"\nFinal parsed state: frame {status['current_frame']} / {status['total_frames']}, "
          f"progress {status['progress']}%")

    print(f"\nPossible progress lines NOT recognized: {len(unmatched)} (first 25)")
    for number, line in unmatched[:25]:
        print(f"  {number:>6}: {line[:160]}")

    if not matched:
        print("\n=> The parser saw no frame progress in this log. Send this output (or the log) to fix it.")


if __name__ == "__main__":
    main()
