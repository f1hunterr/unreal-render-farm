# Unreal render log fixtures

Each `<name>.log` here is fed line by line through the agent's `parse_ue_output`, and the
final parsed state is compared with `<name>.json` (`current_frame`, `total_frames`).

- `synthetic_format.log` is **hand-written** to the formats the parser currently expects.
  It is not real Unreal output.
- Add real logs from render nodes (saved under `C:\UnrealRenderFarm\agent\logs`) to replace guesswork:
  run `python tools/check_ue_log.py <log>` first, then copy the log here with a matching `.json`.
  Trim very large logs to the start, a stretch of progress lines, and the end.
