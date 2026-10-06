# Changelog

## 1.0.0 (2026-10-06)

First public release. Tested on a small Windows farm with Unreal Engine 5.6.

- **Rendering:** Movie Render Queue renders through the farm's own Python executor inside Unreal, with
  exact frame ranges, warm-up frames, the same start time on every computer of a shot, and a check of
  every written frame file.
- **Sharing:** shots are split automatically between the selected free computers (on camera cuts when
  close), or by typed frame ranges; video outputs are skipped on split pieces.
- **Progress:** live phase (loading, warming up, rendering, writing), frame, percent, Unreal's time left
  and seconds per frame.
- **Reliability:** retries on another computer, network-drop retries that don't use a try, freeze watchdog,
  out-of-memory handling, Windows job objects so nothing Unreal started is left running.
- **Speed:** shared Derived Data Cache, Prepare project (1 frame per camera cut, or Epic's cache fill),
  fast mode (-RenderOffscreen), slow-preset notes, workstation mode (artists' PCs render when idle).
- **Output:** a farm output folder for every render, per-render "Save frames to"; presets that save to a
  local drive are redirected to the shared drive.
- **Dashboard:** queue with Retry / Edit / Cancel / Clear finished, History with search, Admin settings;
  strict Content-Security-Policy, login, farm token for agents.
- **Setup:** master in Docker; render PCs with one double-click (SETUP.bat / SETUP-WORKSTATION.bat), offline,
  with protected install folders and safe upgrades.
