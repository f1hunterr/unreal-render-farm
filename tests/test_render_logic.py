import base64
import json
import random
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import farm_agent as agent
from master import farm_master as master

TOKEN = "test-farm-token-123456"
PASSWORD = "test-dash-password-123"


def reset_agent():
    agent._cancel.clear()
    agent._results.clear()
    agent.update_status(
        job_id="", project="", sequence="", scene="", stage="IDLE", progress=0,
        current_frame=0, total_frames=0, fps=0, eta="", start_time=None, last_result=None
    )


def fake_ue(exit_code=0, frames=2, sleep=0.0):
    """Command that behaves like Unreal: prints frame progress, then exits with exit_code"""
    script = (
        "import sys, time\n"
        f"for i in range(1, {frames} + 1):\n"
        f"    print(f'MoviePipeline: Rendering Frame {{i}}/{frames}', flush=True)\n"
        f"    time.sleep({sleep})\n"
        "sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\xff\\xfe not utf-8\\n'); sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n"
    )
    return lambda job, seq: [sys.executable, "-c", script]


def fake_executor_ue(success=True, files=None, exit_code=0, report=True):
    """Command that behaves like Unreal running our executor: tagged progress, then a tagged result.

    Reads -URFStart/-URFEnd from its own command line, like the real executor.
    """
    script = (
        "import sys, json\n"
        "args = dict(a[1:].split('=', 1) for a in sys.argv[1:] if a.startswith('-') and '=' in a)\n"
        "start, end = int(args.get('URFStart', 0)), int(args.get('URFEnd', 9))\n"
        "n = end - start + 1\n"
        "for i in range(1, n + 1):\n"
        "    print('LogPython: URF_PROGRESS ' + json.dumps({'percent': i / n * 100, 'current': i, 'total': n}), flush=True)\n"
        f"files = {files!r} if {files!r} is not None else n\n"
        f"if {report!r}:\n"
        f"    print('LogPython: URF_RESULT ' + json.dumps({{'success': {success!r}, 'files_per_pass': {{'FinalImage': files}}, "
        "'expected_frames': n, 'frame_count_mismatch': files != n, 'error': '' if " + repr(success) + " else 'boom'}), flush=True)\n"
        f"sys.exit({exit_code})\n"
    )
    seen = {}
    real_build_command = agent.build_command  # captured before the test patches it

    def build(job, seq):
        cmd = real_build_command(job, seq)  # real command line, with the executor arguments
        seen["cmd"] = cmd
        return [sys.executable, "-c", script] + [a for a in cmd if a.startswith("-")]
    return build, seen


class SharedExecutorHelperTests(unittest.TestCase):
    def test_task_params(self):
        from urf_mrq_common import task_from_params
        self.assertEqual(task_from_params({"URFStart": "101", "URFEnd": "200", "URFWarmup": "8", "URFJob": "q1-1"}),
                         {"start": 101, "end": 200, "warmup": 8, "job_id": "q1-1", "auto_pieces": 0, "auto_index": 1, "min_chunk": 50, "prepare": False})
        self.assertEqual(task_from_params({})["start"], None)
        with self.assertRaises(ValueError):
            task_from_params({"URFStart": "5"})
        with self.assertRaises(ValueError):
            task_from_params({"URFStart": "9", "URFEnd": "5"})

    def test_plan_auto_split(self):
        from urf_mrq_common import plan_auto_split
        self.assertEqual(plan_auto_split(0, 599, 3), [(0, 199), (200, 399), (400, 599)])
        self.assertEqual(plan_auto_split(0, 99, 4, min_chunk=50), [(0, 49), (50, 99)])   # never below 50
        self.assertEqual(plan_auto_split(10, 40, 8), [(10, 40)])                          # short shot stays whole
        self.assertEqual(plan_auto_split(0, 100, 2), [(0, 50), (51, 100)])
        pieces = plan_auto_split(1001, 1999, 7, min_chunk=10)
        self.assertEqual((pieces[0][0], pieces[-1][1], len(pieces)), (1001, 1999, 7))
        self.assertTrue(all(b + 1 == c for (_, b), (c, _) in zip(pieces, pieces[1:])))

    def test_auto_split_params(self):
        from urf_mrq_common import task_from_params
        task = task_from_params({"URFAutoSplit": "4", "URFMinChunk": "60", "URFJob": "q3-1"})
        self.assertEqual((task["auto_pieces"], task["min_chunk"], task["start"]), (4, 60, None))
        with self.assertRaises(ValueError):
            task_from_params({"URFAutoSplit": "4", "URFStart": "0", "URFEnd": "9"})

    def test_mrq_range_end_exclusive(self):
        from urf_mrq_common import mrq_range
        self.assertEqual(mrq_range(101, 200), (101, 201))

    def test_result_line_flags_mismatch(self):
        from urf_mrq_common import parse_tagged_line, result_line
        kind, data = parse_tagged_line("LogPython: Display: " + result_line(True, {"FinalImage": 99}, 101, 200))
        self.assertEqual(kind, "result")
        self.assertTrue(data["frame_count_mismatch"])
        self.assertEqual(data["expected_frames"], 100)
        self.assertIsNone(parse_tagged_line("LogPython: something else"))


class ExecutorModeTests(unittest.TestCase):
    JOB = {"job_id": "q7-1", "project": "P.uproject", "map": "/Game/M", "config": "/Game/C",
           "sequences": ["/Game/Seq/A"], "frame_start": 101, "frame_end": 110, "warmup": 8}

    def setUp(self):
        reset_agent()
        self.tmp = tempfile.TemporaryDirectory()
        for name, value in (("LOG_DIR", self.tmp.name), ("UE_MODE", "executor")):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_command_line_carries_executor_and_range(self):
        cmd = agent.build_command(self.JOB, "/Game/Seq/A")
        self.assertIn("-ExecutorPythonClass=/Engine/PythonTypes.URFExecutor", cmd)
        self.assertIn("-URFStart=101", cmd)
        self.assertIn("-URFEnd=110", cmd)
        self.assertIn("-URFWarmup=8", cmd)
        self.assertIn("-URFJob=q7-1", cmd)
        env = agent.ue_environment()
        self.assertTrue(env["UE_PYTHONPATH"].startswith(agent.UNREAL_SCRIPTS_DIR))
        self.assertTrue(os.path.isfile(os.path.join(agent.UNREAL_SCRIPTS_DIR, "init_unreal.py")))

    def test_success_reported_by_executor(self):
        build, seen = fake_executor_ue(success=True)
        with mock.patch.object(agent, "build_command", build):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "COMPLETED")
        result = agent._results[-1]
        self.assertEqual((result["frames"], result["frame_start"], result["frame_end"]), (10, 101, 110))
        self.assertEqual(result["detail"], "")
        self.assertEqual(agent.CURRENT_STATUS["last_result"]["status"], "COMPLETED")

    def test_exit_zero_without_executor_result_is_failure(self):
        build, _ = fake_executor_ue(report=False)
        with mock.patch.object(agent, "build_command", build):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "FAILED")
        self.assertIn("Python Editor Script Plugin", agent._results[-1]["detail"])
        self.assertNotIn("URF_PROGRESS", agent._results[-1]["detail"])  # summary keeps Unreal's own lines

    def test_unreal_error_leads_when_executor_never_reported(self):
        script = ("import sys\n"
                  "print('LogWindows: Error: Failed to find Pipeline Configuration asset to render. "
                  "Looked for: /Game/Wrong.Wrong', flush=True)\n"
                  "sys.exit(3)\n")
        with mock.patch.object(agent, "build_command", lambda j, s: [sys.executable, "-c", script]):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "FAILED")
        detail = agent._results[-1]["detail"]
        self.assertTrue(detail.startswith("Unreal stopped (exit code 3). Unreal said: Failed to find Pipeline"))
        self.assertNotIn("Python Editor Script Plugin", detail)

    def test_executor_failure_is_failure_even_with_exit_zero(self):
        build, _ = fake_executor_ue(success=False)
        with mock.patch.object(agent, "build_command", build):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "FAILED")
        self.assertIn("boom", agent._results[-1]["detail"])

    def test_frame_count_mismatch_is_noted(self):
        build, _ = fake_executor_ue(files=9)
        with mock.patch.object(agent, "build_command", build):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "COMPLETED")
        self.assertIn("covers 10 frames", agent._results[-1]["detail"])

    def test_failure_summary_names_the_real_error(self):
        # Lines from a real crash on RENDER-008 (2026-10-03), with the noise around them
        lines = [
            "Error [GENERAL |  | OpenXR-Loader] : Failed to find default runtime with RuntimeInterface::LoadRuntime()",
            "[2026.10.03-08.42.06:357][  0]LogLinker: Warning: [AssetLog] X.uasset: VerifyImport: Failed to find script package",
            "[2026.10.03-08.57.40:950][  4]LogWindows: Error: appError called: Assertion failed: (Index >= 0) & (Index < ArrayNum) [File:D:/build/Array.h] [Line: 1067] ",
            "Array index out of bounds: 1 into an array of size 1",
            "[2026.10.03-08.57.43:574][  4]LogWindows: Error: Assertion failed: (Index >= 0) & (Index < ArrayNum) [File:D:/build/Array.h] [Line: 1067] ",
            "[2026.10.03-08.57.43:574][  4]LogWindows: Error: [Callstack] 0x00007ffc46ee4028 UnrealEditor-Core.dll!UnknownFunction []",
            "[2026.10.03-08.57.43:574][  4]LogWindows: Error: Crash in runnable thread Foreground Worker #1",
        ]
        self.assertEqual(agent.summarize_errors(lines), [
            "Assertion failed: (Index >= 0) & (Index < ArrayNum)",
            "Array index out of bounds: 1 into an array of size 1",
            "Crash in runnable thread Foreground Worker #1"])
        fatal = ["[1][  1]LogWindows: Error: appError called: Fatal error: [File:D:/x.cpp] [Line: 292] ",
                 "[1][  1]LogWindows: Error: Failed to find Pipeline Configuration asset to render. Looked for: /All/Game/Q.uasset"]
        self.assertEqual(agent.summarize_errors(fatal),
                         ["Failed to find Pipeline Configuration asset to render. Looked for: /All/Game/Q.uasset"])

    def test_network_read_error_is_explained(self):
        # Lines from RENDER-006 (2026-10-05): N: dropped while Unreal read the map's BuiltData file
        script = "\n".join([
            "import sys",
            "print('[2026.10.05-07.04.49:745][  0]LogFileManager: Warning: ReadFile failed: Count=0 ReadCount=262144 "
            "LastError=59: An unexpected network error occurred.')",
            "print('[2026.10.05-07.04.49:745][  0]LogLinker: Fatal: [AssetLog] N:/P/Content/Level/S_BuiltData.uasset: "
            "MapBuildDataRegistry /Game/Level/S_BuiltData: Serial size mismatch: Got 268435503, Expected 805306427')",
            "print('[2026.10.05-07.04.49:764][  0]LogWindows: Error: appError called: Assertion failed:  [File:D:/x.cpp] [Line: 4972] ')",
            "print('N:/P/Content/Level/S_BuiltData.uasset: MapBuildDataRegistry /Game/Level/S_BuiltData: Serial size mismatch: Got 268435503, Expected 805306427')",
            "sys.exit(3)",
        ])
        with mock.patch.object(agent, "UE_MODE", "executor"), \
                mock.patch.object(agent, "build_command", lambda j, s: [sys.executable, "-c", script]):
            job = {"job_id": "q1-1", "project": "P", "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S"]}
            self.assertEqual(agent.run_sequence(job, "/Game/S"), "FAILED")
        detail = agent._results[-1]["detail"]
        self.assertIn("Lost the network connection to the project drive", detail)
        self.assertIn("Serial size mismatch", detail)
        self.assertNotIn("Python Editor Script Plugin", detail)
        self.assertEqual(detail.count("Serial size mismatch"), 1)

    def test_status_reports_version_and_features(self):
        client = agent.app.test_client()
        with mock.patch.object(agent, "FARM_TOKEN", TOKEN):
            data = client.get("/status", headers={"X-Farm-Token": TOKEN}).get_json()
        self.assertEqual(data["agent_version"], agent.AGENT_VERSION)
        self.assertIn("frame_range", data["features"])

    def test_auto_split_job_reports_plan_and_renders_first_piece(self):
        job = {"job_id": "q9-1", "project": "P.uproject", "map": "/Game/M", "config": "/Game/C",
               "sequences": ["/Game/Seq/A"], "frame_start": None, "frame_end": None, "warmup": 8,
               "auto_split": {"pieces": 3, "min_chunk": 50}}
        cmd = agent.build_command(job, "/Game/Seq/A")
        self.assertIn("-URFAutoSplit=3", cmd)
        self.assertIn("-URFMinChunk=50", cmd)
        self.assertFalse(any(a.startswith("-URFStart") for a in cmd))
        script = (
            "import json\n"
            "print('LogPython: URF_RANGE ' + json.dumps({'job_id': 'q9-1', 'start': 0, 'end': 599, "
            "'ranges': [[0, 199], [200, 399], [400, 599]], 'note': ''}), flush=True)\n"
            "print('LogPython: URF_RESULT ' + json.dumps({'success': True, 'files_per_pass': {'F': 200}, "
            "'expected_frames': 200, 'frame_count_mismatch': False, 'error': ''}), flush=True)\n")
        with mock.patch.object(agent, "build_command", lambda j, s: [sys.executable, "-c", script]):
            self.assertEqual(agent.run_sequence(job, "/Game/Seq/A"), "COMPLETED")
        result = agent._results[-1]
        self.assertEqual((result["frame_start"], result["frame_end"]), (0, 199))
        self.assertEqual(result["discovered"]["ranges"], [[0, 199], [200, 399], [400, 599]])
        self.assertEqual(agent.CURRENT_STATUS["discovered"]["job_id"], "q9-1")

    def test_validates_auto_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "F.uproject")
            Path(project).write_text("{}")
            base = {"project": project, "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S"]}
            with mock.patch.object(agent, "PROJECT_ROOTS", []):
                job, error = agent.validate_job({**base, "auto_split": {"pieces": 4, "min_chunk": 50}})
                self.assertIsNone(error)
                self.assertEqual(job["auto_split"], {"pieces": 4, "index": 1, "min_chunk": 50})
                for bad in ({"auto_split": {"pieces": 0}}, {"auto_split": {"pieces": 99}},
                            {"auto_split": {"pieces": 2, "index": 3}},
                            {"auto_split": {"pieces": 2}, "frame_start": 0, "frame_end": 9}):
                    with self.subTest(bad=bad):
                        self.assertIsNotNone(agent.validate_job({**base, **bad})[1])

    def test_card_shows_what_unreal_is_doing_while_loading(self):
        # Lines from a real load on RENDER-005 (2026-10-05)
        self.assertEqual(agent.detect_activity(
            "[2026.10.05-06.23.59:251][  0]LogStaticMesh: Display: Waiting for static meshes to be ready 109/115 (/Game/H_244/SM_Car/Mesh_6352) ..."),
            "Building meshes 109/115")
        self.assertEqual(agent.detect_activity(
            "[2026.10.05-06.24.01:661][  0]LogTexture: Display: Building textures: /Game/H_244/Textures/Normal__3_.Normal__3_"),
            "Building textures")
        self.assertIsNone(agent.detect_activity("[2026.10.05-06.24.01:661][  0]LogSlate: nothing interesting"))
        agent.update_status(activity="Building meshes 109/115")
        agent.parse_ue_output("LogStaticMesh: Display: Built static mesh [10.24s] /Game/X")
        self.assertEqual(agent.CURRENT_STATUS["activity"], "Building meshes 109/115")   # keeps the count
        agent.parse_ue_output('LogPython: URF_PROGRESS {"percent": 5.0, "current": 1, "total": 20}')
        self.assertEqual(agent.CURRENT_STATUS["activity"], "Rendering frames")

    def test_shared_ddc_reaches_unreal(self):
        with mock.patch.object(agent, "SHARED_DDC", r"\nas\DDC"):
            self.assertEqual(agent.ue_environment()["UE-SharedDataCachePath"], r"\nas\DDC")

    def test_tagged_progress_drives_status(self):
        agent.update_status(stage="RENDERING", start_time=time.time() - 10)
        self.assertTrue(agent.parse_ue_output('LogPython: URF_PROGRESS {"percent": 50.0, "current": 5, "total": 10}'))
        self.assertEqual((agent.CURRENT_STATUS["progress"], agent.CURRENT_STATUS["current_frame"]), (50.0, 5))

    def test_validates_frame_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "F.uproject")
            Path(project).write_text("{}")
            base = {"project": project, "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S"]}
            with mock.patch.object(agent, "PROJECT_ROOTS", []):
                job, error = agent.validate_job({**base, "frame_start": 0, "frame_end": 99, "warmup": 8})
                self.assertIsNone(error)
                self.assertEqual((job["frame_start"], job["frame_end"], job["warmup"]), (0, 99, 8))
                for bad in ({"frame_start": 5}, {"frame_start": 9, "frame_end": 2},
                            {"frame_start": "0", "frame_end": 9}, {"warmup": -1}):
                    with self.subTest(bad=bad):
                        self.assertIsNotNone(agent.validate_job({**base, **bad})[1])
                with mock.patch.object(agent, "UE_MODE", "legacy"):
                    self.assertIn("URF_UE_MODE", agent.validate_job({**base, "frame_start": 0, "frame_end": 9})[1])


class ParseUeOutputTests(unittest.TestCase):
    def setUp(self):
        reset_agent()
        agent.update_status(stage="RENDERING")

    def test_parses_slash_frame_format(self):
        agent.parse_ue_output("MoviePipeline: Rendering Frame 45/120")
        self.assertEqual(agent.CURRENT_STATUS["current_frame"], 45)
        self.assertEqual(agent.CURRENT_STATUS["total_frames"], 120)
        self.assertEqual(agent.CURRENT_STATUS["progress"], 37.5)

    def test_parses_of_frame_format(self):
        agent.parse_ue_output("MoviePipeline: Rendering Frame 45 of 120")
        self.assertEqual(agent.CURRENT_STATUS["current_frame"], 45)
        self.assertEqual(agent.CURRENT_STATUS["total_frames"], 120)
        self.assertEqual(agent.CURRENT_STATUS["progress"], 37.5)

    def test_finish_message_does_not_mark_completed(self):
        agent.parse_ue_output("MoviePipeline: Finished")
        self.assertEqual(agent.CURRENT_STATUS["stage"], "RENDERING")
        self.assertEqual(agent.CURRENT_STATUS["progress"], 100)

    def test_ignores_impossible_frame_counts(self):
        agent.parse_ue_output("Frame 130/120")
        self.assertEqual(agent.CURRENT_STATUS["current_frame"], 0)


class UeLogFixtureTests(unittest.TestCase):
    """Replay every saved Unreal log in tests/fixtures/ue_logs through the parser"""
    FIXTURES = ROOT / "tests" / "fixtures" / "ue_logs"

    def test_fixtures(self):
        logs = sorted(self.FIXTURES.glob("*.log"))
        self.assertTrue(logs, "no log fixtures found")
        for log_path in logs:
            with self.subTest(log=log_path.name):
                expected = json.loads(log_path.with_suffix(".json").read_text())
                reset_agent()
                agent.update_status(stage="RENDERING")
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        agent.parse_ue_output(line)
                self.assertEqual(agent.CURRENT_STATUS["current_frame"], expected["current_frame"])
                self.assertEqual(agent.CURRENT_STATUS["total_frames"], expected["total_frames"])


class JobValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "Film.uproject")
        Path(self.project).write_text("{}")
        self.job = {
            "project": self.project,
            "map": "/Game/Maps/Main",
            "config": "/Game/Cinematics/Config.Config",
            "sequences": ["/Game/Sequences/Shot010.Shot010"],
        }
        self._roots = agent.PROJECT_ROOTS
        agent.PROJECT_ROOTS = []

    def tearDown(self):
        agent.PROJECT_ROOTS = self._roots
        self.tmp.cleanup()

    def test_accepts_valid_job(self):
        job, error = agent.validate_job(self.job)
        self.assertIsNone(error)
        self.assertEqual(job["sequences"], ["/Game/Sequences/Shot010.Shot010"])

    def test_rejects_switch_injection(self):
        for field, value in (("map", "-ExecCmds=quit"), ("config", "/Game/A -ExecCmds=x"),
                             ("sequences", ["-nullrhi"])):
            with self.subTest(field=field):
                _, error = agent.validate_job({**self.job, field: value})
                self.assertIsNotNone(error)

    def test_rejects_unc_and_non_uproject(self):
        for project in (r"\\evil\share\Film.uproject", self.project + ".exe", "relative/Film.uproject"):
            with self.subTest(project=project):
                _, error = agent.validate_job({**self.job, "project": project})
                self.assertIsNotNone(error)

    def test_enforces_project_roots(self):
        agent.PROJECT_ROOTS = [os.path.normcase(os.path.abspath(os.path.join(self.tmp.name, "allowed")))]
        _, error = agent.validate_job(self.job)
        self.assertIn("outside", error)


class RunSequenceTests(unittest.TestCase):
    JOB = {"job_id": "j1", "project": "P.uproject", "map": "/Game/M", "config": "/Game/C",
           "sequences": ["/Game/Seq/A"]}

    def setUp(self):
        reset_agent()
        self.tmp = tempfile.TemporaryDirectory()
        for name, value in (("LOG_DIR", self.tmp.name), ("UE_MODE", "legacy")):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_exit_zero_is_completed(self):
        with mock.patch.object(agent, "build_command", fake_ue(0)):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "COMPLETED")
        self.assertEqual(agent._results[-1]["frames"], 2)
        self.assertEqual(agent._results[-1]["detail"], "")

    def test_saves_full_render_log(self):
        with mock.patch.object(agent, "build_command", fake_ue(0)):
            agent.run_sequence(self.JOB, "/Game/Seq/A")
        log_file = Path(agent._results[-1]["log_file"])
        self.assertEqual(log_file.parent, Path(self.tmp.name))
        self.assertIn("Rendering Frame 2/2", log_file.read_text(encoding="utf-8"))

    def test_prunes_old_logs(self):
        for i in range(5):
            Path(self.tmp.name, f"20000101-00000{i}_old_A.log").write_text("x")
        with mock.patch.object(agent, "LOGS_KEPT", 3), mock.patch.object(agent, "build_command", fake_ue(0)):
            agent.run_sequence(self.JOB, "/Game/Seq/A")
        self.assertEqual(len(list(Path(self.tmp.name).glob("*.log"))), 3)

    def test_flags_success_without_recognized_progress(self):
        with mock.patch.object(agent, "build_command", fake_ue(0, frames=0)):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "COMPLETED")
        self.assertIn("no frame-progress lines were recognized", agent._results[-1]["detail"])

    def test_unwritable_log_dir_does_not_fail_render(self):
        blocker = Path(self.tmp.name, "file")
        blocker.write_text("x")
        with mock.patch.object(agent, "LOG_DIR", str(blocker / "logs")), \
                mock.patch.object(agent, "build_command", fake_ue(0)):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "COMPLETED")
        self.assertEqual(agent._results[-1]["log_file"], "")

    def test_nonzero_exit_is_failed(self):
        with mock.patch.object(agent, "build_command", fake_ue(3)):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "FAILED")
        self.assertIn("code 3", agent._results[-1]["detail"])

    def test_missing_unreal_is_failed_not_stuck(self):
        with mock.patch.object(agent, "build_command", lambda j, s: [r"C:\nope\UnrealEditor-Cmd.exe"]):
            agent.run_job(self.JOB)
        self.assertEqual(agent._results[-1]["status"], "FAILED")
        self.assertEqual(agent.CURRENT_STATUS["stage"], "IDLE")

    def test_cancel_kills_only_its_process_and_stops_job(self):
        job = {**self.JOB, "sequences": ["/Game/Seq/A", "/Game/Seq/B"]}
        agent.update_status(stage="INITIALIZING")
        with mock.patch.object(agent, "build_command", fake_ue(0, frames=50, sleep=0.1)), \
                mock.patch.object(agent.os, "system") as system:
            worker = threading.Thread(target=agent.run_job, args=(job,))
            worker.start()
            time.sleep(1.0)
            client = agent.app.test_client()
            with mock.patch.object(agent, "FARM_TOKEN", TOKEN):
                resp = client.post("/cancel", headers={"X-Farm-Token": TOKEN})
            self.assertEqual(resp.get_json()["status"], "cancelling")
            worker.join(10)
            system.assert_not_called()
        self.assertFalse(worker.is_alive())
        self.assertEqual([r["status"] for r in agent._results], ["CANCELLED"])
        self.assertEqual(agent.CURRENT_STATUS["stage"], "IDLE")


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        reset_agent()
        self.client = agent.app.test_client()
        patcher = mock.patch.object(agent, "FARM_TOKEN", TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requires_token(self):
        self.assertEqual(self.client.get("/status").status_code, 401)
        self.assertEqual(self.client.get("/status", headers={"X-Farm-Token": "wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/status", headers={"X-Farm-Token": TOKEN}).status_code, 200)

    def test_no_cors_headers(self):
        resp = self.client.get("/status", headers={"X-Farm-Token": TOKEN, "Origin": "http://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)

    def test_busy_node_rejects_second_job(self):
        agent.update_status(stage="RENDERING")
        with mock.patch.object(agent, "validate_job", return_value=({"job_id": "x"}, None)):
            resp = self.client.post("/render", json={}, headers={"X-Farm-Token": TOKEN})
        self.assertEqual(resp.status_code, 409)

    def test_unconfigured_token_fails_closed(self):
        with mock.patch.object(agent, "FARM_TOKEN", ""):
            self.assertEqual(self.client.get("/status", headers={"X-Farm-Token": ""}).status_code, 503)


class ResultIngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        master.init_storage(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def results(self, *seqs):
        return [{"seq": s, "project": "P", "sequence": f"/Game/S{s}", "status": "COMPLETED",
                 "duration": "1m 00s", "frames": 10} for s in seqs]

    def history_count(self):
        with master.connect_db() as conn:
            return conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]

    def test_logs_each_result_once(self):
        self.assertEqual(master.record_results("NodeA", "boot1", self.results(1, 2)), 2)
        self.assertEqual(master.record_results("NodeA", "boot1", self.results(1, 2)), 0)
        self.assertEqual(master.record_results("NodeA", "boot1", self.results(1, 2, 3)), 1)
        self.assertEqual(self.history_count(), 3)

    def test_agent_restart_resets_numbering(self):
        master.record_results("NodeA", "boot1", self.results(1, 2))
        self.assertEqual(master.record_results("NodeA", "boot2", self.results(1)), 1)

    def test_completed_logged_as_success_with_real_project(self):
        master.record_results("NodeA", "boot1", self.results(1))
        with master.connect_db() as conn:
            row = conn.execute("SELECT project, status, duration FROM history").fetchone()
        self.assertEqual(tuple(row), ("P", "SUCCESS", "1m 00s"))


class MasterApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        master.init_storage(self.tmp.name)
        for name, value in (("DASH_PASSWORD", PASSWORD), ("FARM_TOKEN", TOKEN)):
            patcher = mock.patch.object(master, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = master.app.test_client()
        creds = base64.b64encode(f"{master.DASH_USER}:{PASSWORD}".encode()).decode()
        self.auth = {"Authorization": f"Basic {creds}"}

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, body, **headers):
        return self.client.post(url, json=body, headers={**self.auth, **headers})

    def test_requires_login(self):
        self.assertEqual(self.client.get("/get-nodes").status_code, 401)
        self.assertEqual(self.client.get("/get-nodes", headers=self.auth).status_code, 200)

    def test_refuses_cross_origin_post(self):
        resp = self.post("/add-node", {"name": "N1", "ip": "192.168.1.10"}, Origin="http://evil.example")
        self.assertEqual(resp.status_code, 403)

    def test_add_node_validates_and_does_not_overwrite(self):
        self.assertEqual(self.post("/add-node", {"name": "<img src=x>", "ip": "192.168.1.10"}).status_code, 400)
        self.assertEqual(self.post("/add-node", {"name": "N1", "ip": "169.254.169.254"}).status_code, 400)
        self.assertEqual(self.post("/add-node", {"name": "N1", "ip": "8.8.8.8"}).status_code, 400)
        self.assertEqual(self.post("/add-node", {"name": "N1", "ip": "192.168.1.10"}).status_code, 200)
        self.assertEqual(self.post("/add-node", {"name": "N1", "ip": "192.168.1.66"}).status_code, 409)
        self.assertEqual(master.RENDER_NODES["N1"]["ip"], "192.168.1.10")

    def test_corrupt_nodes_file_is_preserved(self):
        Path(master.NODES_FILE).write_text("{not json")
        master.init_storage(self.tmp.name)
        self.assertEqual(master.RENDER_NODES, {})
        self.assertTrue(any(p.name.startswith("nodes.json.corrupt-") for p in Path(self.tmp.name).iterdir()))

    def test_dashboard_served_with_strict_csp(self):
        with self.client.get("/", headers=self.auth) as resp:
            self.assertEqual(resp.status_code, 200)
            csp = resp.headers["Content-Security-Policy"]
            html = resp.get_data(as_text=True)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("style=", html)
        with self.client.get("/static/dashboard.js", headers=self.auth) as resp:
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/static/dashboard.js").status_code, 401)

    def test_launch_validates_priority_and_retries(self):
        base = {"project": "C:/P.uproject", "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S1"]}
        self.assertEqual(self.post("/launch", {**base, "priority": 7}).status_code, 400)
        self.assertEqual(self.post("/launch", {**base, "retries": 99}).status_code, 400)
        self.assertEqual(self.post("/launch", {**base, "nodes": ["Ghost"]}).status_code, 400)


class QueueFixture:
    """Master with two idle nodes A and B and a fake agent HTTP layer"""
    BATCH = {"project": "C:/P.uproject", "map": "/Game/M", "config": "/Game/C"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        master.init_storage(self.tmp.name)
        for name, value in (("DASH_PASSWORD", PASSWORD), ("FARM_TOKEN", TOKEN)):
            patcher = mock.patch.object(master, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = master.app.test_client()
        creds = base64.b64encode(f"{master.DASH_USER}:{PASSWORD}".encode()).decode()
        self.auth = {"Authorization": f"Basic {creds}"}
        for name, ip in (("A", "192.168.1.10"), ("B", "192.168.1.11")):
            self.post("/add-node", {"name": name, "ip": ip})
            self.set_stage(name, "IDLE")
        self.sent = []
        self.responses = {}
        patcher = mock.patch.object(master.HTTP, "post", side_effect=self.fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, body):
        return self.client.post(url, json=body, headers=self.auth)

    def set_stage(self, node, stage, **extra):
        # Test computers run a current agent (can render frame ranges) unless a test says otherwise
        master.STATUS_CACHE[node] = {**master.offline_status(node, stage), "features": ["frame_range", "auto_split", "auto_piece"], **extra}

    def fake_post(self, url, json=None, headers=None, timeout=None):
        node = "A" if "192.168.1.10" in url else "B"
        if url.endswith("/render"):
            self.sent.append((node, json))
        resp = self.responses.get(node) or mock.Mock(ok=True, status_code=200)
        return resp

    def queue(self, sequences, **kw):
        kw.setdefault("auto_split", False)   # tests share only when they ask to
        return self.post("/launch", {**self.BATCH, "sequences": sequences, **kw}).get_json()

    def jobs(self):
        return {j["id"]: j for j in self.client.get("/get-queue", headers=self.auth).get_json()["jobs"]}

    def report(self, node, status, attempt_job_id, seq_no):
        result = {"seq": seq_no, "job_id": attempt_job_id, "project": "C:/P.uproject",
                  "sequence": "/Game/S1", "status": status, "detail": "boom" if status == "FAILED" else ""}
        master.record_results(node, f"boot-{node}", [result])
        self.set_stage(node, "IDLE")


class QueueTests(QueueFixture, unittest.TestCase):
    def test_idle_nodes_pull_jobs_in_priority_order(self):
        self.queue(["/Game/Low"], priority=2)
        self.queue(["/Game/Rush"], priority=0)
        self.queue(["/Game/Normal"], priority=1)
        master.schedule_jobs()
        sent = {node: payload["sequences"][0] for node, payload in self.sent}
        self.assertEqual(sent, {"A": "/Game/Rush", "B": "/Game/Normal"})
        self.assertEqual(self.sent[0][1]["job_id"], "q2-1")  # queue #2 (Rush), attempt 1
        statuses = {j["sequence"]: j["status"] for j in self.jobs().values()}
        self.assertEqual(statuses, {"/Game/Rush": "ASSIGNED", "/Game/Normal": "ASSIGNED", "/Game/Low": "QUEUED"})

    def test_busy_node_gets_no_second_job(self):
        self.queue(["/Game/S1", "/Game/S2"], nodes=["A"])
        master.schedule_jobs()
        self.set_stage("A", "IDLE")  # stale poll: still says IDLE, but A already has an assigned job
        master.schedule_jobs()
        self.assertEqual(len(self.sent), 1)

    def test_failed_job_retries_on_another_node(self):
        self.queue(["/Game/S1"], retries=2)
        self.set_stage("B", "RENDERING")
        master.schedule_jobs()
        self.assertEqual(self.sent[-1][0], "A")
        self.set_stage("B", "IDLE")
        self.report("A", "FAILED", "q1-1", 1)
        job = self.jobs()[1]
        self.assertEqual((job["status"], job["attempts"]), ("QUEUED", 1))
        self.assertIn("failed on A", job["detail"])
        master.schedule_jobs()
        self.assertEqual(self.sent[-1][0], "B")
        self.assertEqual(self.sent[-1][1]["job_id"], "q1-2")

    def test_retry_reuses_same_node_when_no_other_is_idle(self):
        self.queue(["/Game/S1"], nodes=["A"], retries=1)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        master.schedule_jobs()
        self.assertEqual([n for n, _ in self.sent], ["A", "A"])

    def test_gives_up_after_max_attempts(self):
        self.queue(["/Game/S1"], nodes=["A"], retries=1)
        for attempt in (1, 2):
            master.schedule_jobs()
            self.report("A", "FAILED", f"q1-{attempt}", attempt)
        master.schedule_jobs()
        job = self.jobs()[1]
        self.assertEqual((job["status"], job["attempts"], len(self.sent)), ("FAILED", 2, 2))

    def test_network_drop_retries_without_using_a_try(self):
        self.queue(["/Game/S1"], nodes=["A"], retries=0)      # no normal retries at all
        for attempt in range(1, 4):
            master.schedule_jobs()
            master.record_results("A", "boot-A", [{"seq": attempt, "job_id": f"q1-{attempt}", "project": "P",
                                                   "sequence": "/Game/S1", "status": "FAILED",
                                                   "detail": "Lost the network connection...", "network_error": True}])
            self.set_stage("A", "IDLE")
            job = self.jobs()[1]
            self.assertEqual((job["status"], job["attempts"]), ("QUEUED", 0))
            self.assertIn(f"({attempt} of 3, not counted", job["detail"])
        master.schedule_jobs()                                # a 4th network drop now fails the job
        master.record_results("A", "boot-A", [{"seq": 4, "job_id": "q1-4", "project": "P", "sequence": "/Game/S1",
                                               "status": "FAILED", "detail": "x", "network_error": True}])
        self.assertEqual(self.jobs()[1]["status"], "FAILED")

    def test_success_and_late_success(self):
        self.queue(["/Game/S1"], nodes=["A"])
        master.schedule_jobs()
        self.report("A", "COMPLETED", "q1-1", 1)
        self.assertEqual(self.jobs()[1]["status"], "SUCCESS")

    def test_rejection_counts_as_attempt(self):
        rejected = mock.Mock(ok=False, status_code=400, reason="Bad Request")
        rejected.json.return_value = {"error": "project file not found on this node"}
        self.responses["A"] = rejected
        self.queue(["/Game/S1"], retries=1)
        self.set_stage("B", "RENDERING")
        master.schedule_jobs()
        job = self.jobs()[1]
        self.assertEqual((job["status"], job["attempts"]), ("QUEUED", 1))
        self.assertIn("project file not found", job["detail"])

    def test_busy_reply_leaves_job_queued_without_attempt(self):
        self.responses["A"] = mock.Mock(ok=False, status_code=409)
        self.queue(["/Game/S1"], nodes=["A"])
        master.schedule_jobs()
        job = self.jobs()[1]
        self.assertEqual((job["status"], job["attempts"]), ("QUEUED", 0))

    def test_lost_job_is_requeued(self):
        self.queue(["/Game/S1"], nodes=["A"])
        master.schedule_jobs()
        # A restarted: the next poll finds it IDLE, but it never reported a result
        self.set_stage("A", "IDLE")
        master.schedule_jobs()
        self.assertEqual(len(self.sent), 1)  # within the grace period nothing changes
        master.schedule_jobs(now=time.time() + master.LOST_JOB_GRACE_SECONDS + 1)
        self.assertEqual(self.sent[-1][1]["job_id"], "q1-2")

    def test_long_offline_node_job_is_requeued(self):
        self.queue(["/Game/S1"], nodes=["A", "B"])
        self.set_stage("B", "RENDERING")
        master.schedule_jobs()
        self.set_stage("A", "OFFLINE", offline_since=time.time())
        self.set_stage("B", "IDLE")
        master.schedule_jobs()
        self.assertEqual(len(self.sent), 1)  # a short outage keeps the assignment
        master.schedule_jobs(now=time.time() + master.OFFLINE_REQUEUE_SECONDS + 1)
        self.assertEqual(self.sent[-1][0], "B")

    def test_cancel_queued_and_running_jobs(self):
        self.queue(["/Game/S1", "/Game/S2"], nodes=["A"])
        master.schedule_jobs()
        self.assertEqual(self.post("/cancel-job", {"id": 2}).get_json()["status"], "cancelled")
        self.assertEqual(self.post("/cancel-job", {"id": 1}).get_json()["status"], "cancelling")
        self.report("A", "FAILED", "q1-1", 1)  # killed render reports failure: must not be retried
        jobs = self.jobs()
        self.assertEqual((jobs[1]["status"], jobs[2]["status"]), ("CANCELLED", "CANCELLED"))
        master.schedule_jobs()
        self.assertEqual(len(self.sent), 1)

    def test_retry_window_shows_settings_and_applies_fixes(self):
        self.queue(["/Game/S1"], nodes=["A"], retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        job = self.client.get("/get-job?id=1", headers=self.auth).get_json()
        self.assertEqual((job["status"], job["config"], job["allowed_nodes"], job["retries"]),
                         ("FAILED", "/Game/C", ["A"], 0))
        self.assertIn("boom", job["detail"])                       # the reason is shown in the window
        changes = {"config": "/All/Game/Cinematics/Fixed.uasset", "project": '"C:\\Fixed.uproject"',
                   "priority": 0, "retries": 2, "nodes": ["A", "B"], "sequence": "/Game/S1", "frames": "",
                   "share": False}
        resp = self.post("/retry-job", {"id": 1, "changes": changes}).get_json()
        self.assertEqual(sorted(resp["changed"]), ["allowed_nodes", "config", "max_attempts", "priority", "project"])
        job = self.client.get("/get-job?id=1", headers=self.auth).get_json()
        self.assertEqual((job["status"], job["config"], job["project"], job["priority"], job["retries"]),
                         ("QUEUED", "/Game/Cinematics/Fixed", "C:\\Fixed.uproject", 0, 2))
        self.assertIn("Retried with new", job["detail"])
        master.schedule_jobs()
        self.assertEqual(self.sent[-1][1]["config"], "/Game/Cinematics/Fixed")

    def test_retry_with_bad_project_is_refused(self):
        self.queue(["/Game/S1"], nodes=["A"], retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        resp = self.post("/retry-job", {"id": 1, "changes": {"project": "D:\\Template\\Countryside"}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.jobs()[1]["status"], "FAILED")

    def test_retry_can_switch_on_sharing(self):
        self.queue(["/Game/S1"], nodes=["A"], retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        self.post("/retry-job", {"id": 1, "changes": {"frames": "", "share": True}})
        job = self.client.get("/get-job?id=1", headers=self.auth).get_json()
        self.assertEqual((job["split_mode"], job["frame_start"], job["warmup"]), ("auto", None, 8))

    def test_retry_of_a_piece_keeps_its_frames(self):
        self.queue([{"path": "/Game/Long", "frames": "0-199"}], chunk_size=100, retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        self.post("/retry-job", {"id": 1, "changes": {"sequence": "/Game/Other", "frames": "5-6",
                                                      "config": "/Game/Fixed.Fixed"}})
        job = self.client.get("/get-job?id=1", headers=self.auth).get_json()
        self.assertEqual((job["sequence"], job["frame_start"], job["frame_end"], job["config"], job["is_piece"]),
                         ("/Game/Long", 0, 99, "/Game/Fixed.Fixed", True))

    def test_shot_retry_window(self):
        self.queue([{"path": "/Game/Long", "frames": "0-199"}], chunk_size=100, retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        self.report("B", "COMPLETED", "q2-1", 1)
        shot_id = self.jobs()[1]["shot_id"]
        job = self.client.get(f"/get-job?shot_id={shot_id}", headers=self.auth).get_json()
        self.assertEqual((job["id"], job["status"]), (1, "FAILED"))  # the failed piece, with its reason
        resp = self.post("/retry-shot", {"shot_id": shot_id, "changes": {"config": "/Game/Fixed.Fixed"}}).get_json()
        self.assertEqual(resp["jobs"], 1)
        jobs = self.jobs()
        self.assertEqual((jobs[1]["status"], jobs[2]["status"]), ("QUEUED", "SUCCESS"))
        self.assertEqual(self.client.get("/get-job?id=1", headers=self.auth).get_json()["config"], "/Game/Fixed.Fixed")
        self.assertEqual(self.client.get("/get-job?id=2", headers=self.auth).get_json()["config"], "/Game/C")

    def test_manual_retry(self):
        self.queue(["/Game/S1"], nodes=["A"], retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        self.assertEqual(self.post("/retry-job", {"id": 1}).status_code, 200)
        self.assertEqual(self.post("/retry-job", {"id": 1}).status_code, 409)
        master.schedule_jobs()
        self.assertEqual(self.sent[-1][1]["job_id"], "q1-1")


class FrameSplittingTests(QueueFixture, unittest.TestCase):
    PLAN = {"start": 0, "end": 599, "ranges": [[0, 299], [300, 599]], "note": ""}

    def test_shared_shot_starts_on_every_free_computer_at_once(self):
        data = self.queue(["/Game/Long"], auto_split=True)
        self.assertEqual((data["jobs"], data["auto"]), (1, 1))
        master.schedule_jobs()          # piece 1 goes out and the shot becomes 2 pieces
        master.schedule_jobs()          # piece 2 goes out straight away (no waiting for piece 1)
        sent = [(node, p["auto_split"]) for node, p in self.sent]
        self.assertEqual(sent, [("A", {"pieces": 2, "index": 1, "min_chunk": master.AUTO_MIN_CHUNK}),
                                ("B", {"pieces": 2, "index": 2, "min_chunk": master.AUTO_MIN_CHUNK})])
        self.assertTrue(all("frame_start" not in p for _, p in self.sent))
        jobs = sorted(self.jobs().values(), key=lambda j: j["id"])
        self.assertEqual([(j["chunk_index"], j["chunk_count"], j["split_mode"], j["status"]) for j in jobs],
                         [(1, 2, "auto-piece", "ASSIGNED"), (2, 2, "auto-piece", "ASSIGNED")])

    def test_reported_plan_records_each_pieces_frames(self):
        self.queue(["/Game/Long"], auto_split=True)
        master.schedule_jobs(); master.schedule_jobs()
        self.assertEqual(master.expand_auto_job("q1-1", {**self.PLAN, "index": 1, "job_id": "q1-1"}), 2)
        frames = sorted((j["chunk_index"], j["frame_start"], j["frame_end"]) for j in self.jobs().values())
        self.assertEqual(frames, [(1, 0, 299), (2, 300, 599)])
        shot = self.client.get("/get-queue", headers=self.auth).get_json()["shots"][0]
        self.assertEqual((shot["chunks"], shot["frame_start"], shot["frame_end"]), (2, 0, 599))

    def test_retried_piece_keeps_its_frames(self):
        self.queue(["/Game/Long"], auto_split=True, retries=1)
        master.schedule_jobs(); master.schedule_jobs()
        master.expand_auto_job("q2-1", {**self.PLAN, "index": 2, "job_id": "q2-1"})
        self.report("B", "FAILED", "q2-1", 1)
        master.schedule_jobs()
        retry = [p for _, p in self.sent if p["job_id"] == "q2-2"][0]
        self.assertEqual((retry["frame_start"], retry["frame_end"]), (300, 599))  # explicit range now
        self.assertNotIn("auto_split", retry)

    def test_plan_from_result_is_the_fallback(self):
        self.queue(["/Game/Long"], auto_split=True)
        master.schedule_jobs(); master.schedule_jobs()
        master.record_results("A", "boot-A", [{"seq": 1, "job_id": "q1-1", "project": "P", "sequence": "/Game/Long",
                                               "status": "COMPLETED", "frame_start": 0, "frame_end": 299,
                                               "discovered": {**self.PLAN, "index": 1, "job_id": "q1-1"}}])
        jobs = self.jobs()
        self.assertEqual((jobs[1]["status"], jobs[2]["frame_start"]), ("SUCCESS", 300))

    def test_short_shot_marks_extra_pieces_not_needed(self):
        self.set_stage("B", "RENDERING")          # piece 2 cannot start yet
        self.queue(["/Game/Short"], auto_split=True)
        master.schedule_jobs()
        short = {"job_id": "q1-1", "index": 1, "start": 0, "end": 30, "ranges": [[0, 30]], "note": ""}
        self.assertEqual(master.expand_auto_job("q1-1", short), 1)
        jobs = sorted(self.jobs().values(), key=lambda j: j["id"])
        self.assertEqual(jobs[0]["frame_start"], 0)
        self.assertEqual(jobs[1]["status"], "SUCCESS")
        self.assertIn("Not needed", jobs[1]["detail"])

    def test_single_computer_renders_whole_shot(self):
        self.set_stage("B", "OFFLINE")
        self.queue(["/Game/Long"], auto_split=True)
        master.schedule_jobs()
        self.assertEqual(self.sent[0][1]["auto_split"], {"pieces": 1, "index": 1, "min_chunk": master.AUTO_MIN_CHUNK})
        self.assertEqual(len(self.jobs()), 1)

    def test_bad_plan_is_ignored(self):
        self.queue(["/Game/Long"], auto_split=True)
        master.schedule_jobs(); master.schedule_jobs()
        gap = {"job_id": "q1-1", "index": 1, "start": 0, "end": 599, "ranges": [[0, 199], [300, 599]]}
        self.assertEqual(master.expand_auto_job("q1-1", gap), 0)
        self.assertTrue(all(j["frame_start"] is None for j in self.jobs().values()))

    def test_auto_shot_skips_agents_that_cannot_share(self):
        self.set_stage("A", "IDLE", features=["frame_range", "auto_split"])   # one build too old
        self.set_stage("B", "IDLE", features=["frame_range"])
        self.queue(["/Game/Long"], auto_split=True)
        master.schedule_jobs()
        self.assertEqual(self.sent, [])
        self.assertIn("SETUP.bat", self.jobs()[1]["detail"])

    def test_typed_frames_with_sharing_pick_piece_size(self):
        data = self.queue([{"path": "/Game/Long", "frames": "0-999"}], auto_split=True)
        self.assertEqual(data["jobs"], 2)  # 2 usable computers -> 2 pieces of 500
        ranges = sorted((j["frame_start"], j["frame_end"]) for j in self.jobs().values())
        self.assertEqual(ranges, [(0, 499), (500, 999)])

    def test_request_without_share_setting_is_shared(self):
        # An old dashboard page (before the Share box) sends no auto_split: share, like the form's default
        resp = self.post("/launch", {**self.BATCH, "sequences": ["/Game/Long"]}).get_json()
        self.assertEqual((resp["auto"], resp["sharing_off"]), (1, False))
        resp = self.post("/launch", {**self.BATCH, "sequences": ["/Game/Long2"], "auto_split": False}).get_json()
        self.assertEqual((resp["auto"], resp["sharing_off"]), (0, True))

    def test_only_selected_computers_share_the_shot(self):
        data = self.queue(["/Game/Long"], auto_split=True, nodes=["A"])     # B is free but not selected
        self.assertEqual(data["sharing"], {"computers": ["A"], "left_out": []})
        master.schedule_jobs(); master.schedule_jobs()
        self.assertEqual([n for n, _ in self.sent], ["A"])
        self.assertEqual(self.sent[0][1]["auto_split"]["pieces"], 1)
        self.assertEqual(len(self.jobs()), 1)

    def test_send_reply_names_selected_computers_that_cannot_share(self):
        self.set_stage("B", "IDLE", features=["frame_range"])          # older agent
        data = self.queue(["/Game/Long"], auto_split=True, nodes=["A", "B"])
        self.assertEqual(data["sharing"]["computers"], ["A"])
        self.assertEqual(data["sharing"]["left_out"], [{"node": "B", "why": "needs the latest SETUP.bat"}])
        self.set_stage("B", "OFFLINE")
        data = self.queue(["/Game/Long2"], auto_split=True, nodes=["A", "B"])
        self.assertEqual(data["sharing"]["left_out"], [{"node": "B", "why": "offline"}])

    def test_typed_frames_split_across_older_agents_too(self):
        # Typed frames only need "frame_range", which older agents (like RENDER-07/008 on 2026-10-05) have
        self.set_stage("A", "IDLE", features=["frame_range", "executor"])
        self.set_stage("B", "IDLE", features=["frame_range", "executor"])
        data = self.queue([{"path": "/Game/Long", "frames": "0-470"}], auto_split=True, nodes=["A", "B"])
        self.assertEqual(data["jobs"], 2)
        self.assertEqual(data["sharing"]["computers"], ["A", "B"])
        master.schedule_jobs()
        self.assertEqual(sorted((n, p["frame_start"], p["frame_end"]) for n, p in self.sent),
                         [("A", 0, 235), ("B", 236, 470)])

    def test_replies_carry_dashboard_version(self):
        resp = self.client.get("/get-nodes", headers=self.auth)
        self.assertRegex(resp.headers["X-Farm-UI"], r"^[0-9a-f]{12}$")

    def test_parse_and_split(self):
        self.assertEqual(master.parse_frame_range(" 0 - 249 "), (0, 249))
        self.assertEqual(master.parse_frame_range("10:20"), (10, 20))
        self.assertIsNone(master.parse_frame_range(""))
        for bad in ("abc", "20-10", "5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                master.parse_frame_range(bad)
        self.assertEqual(master.split_frames(0, 249, 100), [(0, 99), (100, 199), (200, 249)])
        self.assertEqual(master.split_frames(5, 5, 100), [(5, 5)])
        self.assertEqual(master.split_frames(0, 249, 0), [(0, 249)])

    def test_split_shot_renders_on_several_nodes_at_once(self):
        data = self.queue([{"path": "/Game/Long", "frames": "0-249"}], chunk_size=100)
        self.assertEqual((data["shots"], data["jobs"]), (1, 3))
        master.schedule_jobs()
        sent = {node: (p["frame_start"], p["frame_end"], p["warmup"]) for node, p in self.sent}
        self.assertEqual(sent, {"A": (0, 99, 8), "B": (100, 199, 8)})
        queue = self.client.get("/get-queue", headers=self.auth).get_json()
        shot = queue["shots"][0]
        self.assertEqual((shot["chunks"], shot["rendering"], shot["queued"]), (3, 2, 1))
        self.assertEqual((shot["frame_start"], shot["frame_end"]), (0, 249))
        history = self.client.get("/get-history?dispatched=1", headers=self.auth).get_json()
        self.assertIn("/Game/Long [0-99]", {h["sequence"] for h in history})

    def test_accepts_pasted_paths(self):
        self.assertEqual(master.clean_pasted('"N:\\Projects\\Film\\Film.uproject"'), "N:\\Projects\\Film\\Film.uproject")
        self.assertEqual(master.clean_pasted("/Script/Engine.World'/Game/Maps/Main.Main'"), "/Game/Maps/Main.Main")
        self.assertEqual(master.clean_pasted(" /Game/Seq/Shot010 "), "/Game/Seq/Shot010")
        self.post("/launch", {"project": '"C:\\P.uproject"', "map": "/Script/Engine.World'/Game/M.M'",
                              "config": "/Script/MovieRenderPipelineCore.MoviePipelinePrimaryConfig'/Game/C.C'",
                              "sequences": [{"path": "/Script/LevelSequence.LevelSequence'/Game/S1.S1'", "frames": ""}]})
        master.schedule_jobs()
        payload = self.sent[0][1]
        self.assertEqual((payload["project"], payload["map"], payload["config"], payload["sequences"]),
                         ("C:\\P.uproject", "/Game/M.M", "/Game/C.C", ["/Game/S1.S1"]))

    def test_cleans_paths_artists_actually_pasted(self):
        # Taken from real attempts on the farm (2026-10-03)
        self.assertEqual(master.clean_asset("/All/Game/MoviePipelineQueue_RenderFarm_Test.uasset"),
                         "/Game/MoviePipelineQueue_RenderFarm_Test")
        self.assertEqual(master.clean_asset("K:\\Template\\Country_side\\Content\\Test\\DIS_S01.uasset"),
                         "/Game/Test/DIS_S01")
        self.assertEqual(master.clean_asset("/Game/Level/Main_Street.umap"), "/Game/Level/Main_Street")
        self.assertEqual(master.clean_asset("/Game/Sequencer/Scene_01/Scene_01.Scene_01"),
                         "/Game/Sequencer/Scene_01/Scene_01.Scene_01")
        self.assertEqual(master.clean_project("N:\\Sys\\MyFilm.uproject.uproject"), "N:\\Sys\\MyFilm.uproject")
        resp = self.post("/launch", {**self.BATCH, "project": "D:\\Template\\Country_side\\Countryside",
                                     "sequences": ["/Game/S1"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(".uproject", resp.get_json()["error"])

    def test_frame_range_jobs_wait_for_new_agents(self):
        self.set_stage("A", "IDLE", features=[])          # both computers run old agents
        self.set_stage("B", "IDLE", features=[])
        self.queue([{"path": "/Game/Long", "frames": "0-99"}], chunk_size=100)
        master.schedule_jobs()
        self.assertEqual(self.sent, [])                    # never sent to an old agent
        self.assertIn("SETUP.bat", self.jobs()[1]["detail"])
        self.set_stage("B", "IDLE", features=["frame_range"])  # B gets the new SETUP.bat
        master.schedule_jobs()
        self.assertEqual([n for n, _ in self.sent], ["B"])

    def test_whole_shot_without_chunking_sends_no_range(self):
        self.queue(["/Game/S1"])
        master.schedule_jobs()
        self.assertNotIn("frame_start", self.sent[0][1])
        self.assertNotIn("warmup", self.sent[0][1])

    def test_range_without_chunking_is_one_task(self):
        data = self.queue([{"path": "/Game/S1", "frames": "50-80"}])
        self.assertEqual(data["jobs"], 1)
        master.schedule_jobs()
        self.assertEqual((self.sent[0][1]["frame_start"], self.sent[0][1]["frame_end"]), (50, 80))

    def test_shot_without_frames_renders_whole_in_split_batch(self):
        data = self.queue([{"path": "/Game/Whole", "frames": ""}, {"path": "/Game/Long", "frames": "0-199"}],
                          chunk_size=100)
        self.assertEqual((data["shots"], data["jobs"]), (2, 3))
        whole = [j for j in self.jobs().values() if j["sequence"] == "/Game/Whole"]
        self.assertEqual((len(whole), whole[0]["frame_start"]), (1, None))

    def test_too_many_chunks_refused(self):
        resp = self.post("/launch", {**self.BATCH, "sequences": [{"path": "/Game/S1", "frames": "0-1000000"}],
                                     "chunk_size": 1})
        self.assertEqual(resp.status_code, 400)

    def test_retry_shot_requeues_only_failed_chunks(self):
        self.queue([{"path": "/Game/S1", "frames": "0-199"}], chunk_size=100, retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        self.report("B", "COMPLETED", "q2-1", 1)
        shot_id = self.jobs()[1]["shot_id"]
        self.assertEqual(self.post("/retry-shot", {"shot_id": shot_id}).get_json()["jobs"], 1)
        jobs = self.jobs()
        self.assertEqual((jobs[1]["status"], jobs[2]["status"]), ("QUEUED", "SUCCESS"))

    def test_queue_shows_live_progress_and_shot_percent(self):
        self.queue([{"path": "/Game/Long", "frames": "0-399"}], chunk_size=100)
        master.schedule_jobs()
        self.set_stage("A", "RENDERING", job_id="q1-1", progress=50.0)
        self.set_stage("B", "RENDERING", job_id="q2-1", progress=10.0)
        queue = self.client.get("/get-queue", headers=self.auth).get_json()
        progress = {j["id"]: j["progress"] for j in queue["jobs"]}
        self.assertEqual((progress[1], progress[2], progress[3]), (50.0, 10.0, None))
        self.assertEqual(queue["shots"][0]["percent"], 15.0)  # (0.5 + 0.1) of 4 pieces
        self.report("A", "COMPLETED", "q1-1", 1)
        queue = self.client.get("/get-queue", headers=self.auth).get_json()
        self.assertEqual(queue["shots"][0]["percent"], 27.5)  # 1 done + 0.1 of 4

    def test_history_hides_dispatch_events_and_searches(self):
        self.queue(["/Game/S1", "/Game/S2"])
        master.schedule_jobs()
        self.report("A", "COMPLETED", "q1-1", 1)
        default = self.client.get("/get-history", headers=self.auth).get_json()
        self.assertEqual({h["status"] for h in default}, {"SUCCESS"})
        everything = self.client.get("/get-history?dispatched=1", headers=self.auth).get_json()
        self.assertIn("DISPATCHED", {h["status"] for h in everything})
        self.assertEqual(len(self.client.get("/get-history?q=RENDER-none", headers=self.auth).get_json()), 0)
        self.assertEqual(len(self.client.get("/get-history?q=s1", headers=self.auth).get_json()), 1)
        self.assertEqual(self.client.get("/get-history?q=100%25_", headers=self.auth).status_code, 200)

    def test_status_reports_offline_since(self):
        master.STATUS_CACHE["B"] = {**master.offline_status("B"), "offline_since": time.time() - 60}
        status = {n["node"]: n for n in self.client.get("/status", headers=self.auth).get_json()}
        self.assertRegex(status["B"]["offline_since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertNotIn("offline_since", status["A"])

    def test_cancel_shot(self):
        self.queue([{"path": "/Game/S1", "frames": "0-299"}], chunk_size=100)
        master.schedule_jobs()
        shot_id = self.jobs()[1]["shot_id"]
        self.assertEqual(self.post("/cancel-shot", {"shot_id": shot_id}).get_json()["jobs"], 3)
        self.assertEqual(self.jobs()[3]["status"], "CANCELLED")  # the queued one is cancelled at once


class SchemaMigrationTests(unittest.TestCase):
    def test_old_database_gains_frame_columns(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(os.path.join(tmp, "render_farm.db"))
            # The jobs table exactly as the previous version (commit e34ca1d) created it
            conn.execute('''CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, project TEXT,
                map TEXT, config TEXT, sequence TEXT, priority INTEGER DEFAULT 1, status TEXT DEFAULT 'QUEUED',
                attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, allowed_nodes TEXT DEFAULT '[]',
                tried_nodes TEXT DEFAULT '[]', node_name TEXT DEFAULT '', agent_job_id TEXT DEFAULT '',
                cancel_requested INTEGER DEFAULT 0, detail TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
                assigned_at REAL DEFAULT 0)''')
            conn.execute("INSERT INTO jobs (sequence, status) VALUES ('/Game/Old', 'SUCCESS')")
            conn.commit()
            conn.close()
            master.init_storage(tmp)
            with master.connect_db() as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                old = conn.execute("SELECT sequence, chunk_count, frame_start FROM jobs").fetchone()
            self.assertTrue({"frame_start", "frame_end", "warmup", "shot_id", "chunk_index", "chunk_count"} <= columns)
            self.assertEqual(tuple(old), ("/Game/Old", 1, None))


class SelfRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        master.init_storage(self.tmp.name)
        for name, value in (("DASH_PASSWORD", PASSWORD), ("FARM_TOKEN", TOKEN)):
            patcher = mock.patch.object(master, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The master probes the node back; by default pretend a firewall blocks it
        self.probe = mock.patch.object(master.HTTP, "get", side_effect=master.requests.ConnectionError("blocked"))
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.client = master.app.test_client()

    def test_reports_whether_master_can_reach_node(self):
        reply = self.register("RENDER-010", "192.168.1.10").get_json()
        self.assertIs(reply["reachable"], False)
        self.probe.stop()
        with mock.patch.object(master.HTTP, "get", return_value=mock.Mock(ok=True)):
            self.assertIs(self.register("RENDER-010", "192.168.1.10").get_json()["reachable"], True)
        self.probe.start()

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, name, ip, token=TOKEN):
        return self.client.post("/register-node", json={"name": name, "ip": ip}, headers={"X-Farm-Token": token})

    def test_requires_farm_token_not_dashboard_login(self):
        self.assertEqual(self.register("RENDER-010", "192.168.1.10", token="wrong").status_code, 401)
        self.assertEqual(self.register("RENDER-010", "192.168.1.10").get_json()["status"], "registered")
        self.assertEqual(master.RENDER_NODES["RENDER-010"]["ip"], "192.168.1.10")
        self.assertEqual(self.register("RENDER-010", "192.168.1.10").get_json()["status"], "unchanged")

    def test_validates_name_and_ip(self):
        self.assertEqual(self.register("<script>", "192.168.1.10").status_code, 400)
        self.assertEqual(self.register("N1", "8.8.8.8").status_code, 400)

    def test_ip_change_refused_while_old_address_is_online(self):
        self.register("RENDER-010", "192.168.1.10")
        master.STATUS_CACHE["RENDER-010"] = master.offline_status("RENDER-010", "IDLE")
        self.assertEqual(self.register("RENDER-010", "192.168.1.99").status_code, 409)
        self.assertEqual(master.RENDER_NODES["RENDER-010"]["ip"], "192.168.1.10")

    def test_ip_change_allowed_when_old_address_is_offline(self):
        self.register("RENDER-010", "192.168.1.10")
        master.STATUS_CACHE["RENDER-010"] = master.offline_status("RENDER-010", "OFFLINE")
        self.assertEqual(self.register("RENDER-010", "192.168.1.99").get_json()["status"], "updated")
        self.assertEqual(master.RENDER_NODES["RENDER-010"]["ip"], "192.168.1.99")

    def test_agent_sends_name_ip_and_token(self):
        sent = {}

        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return b'{"status": "registered"}'

        def fake_urlopen(req, timeout):
            sent.update(url=req.full_url, token=req.get_header("X-farm-token"), body=json.loads(req.data))
            return Reply()

        with mock.patch.object(agent, "MASTER_URL", "http://127.0.0.1:5000"), \
                mock.patch.object(agent, "FARM_TOKEN", TOKEN), \
                mock.patch.object(agent.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(agent.register_with_master(), {"status": "registered"})
        self.assertEqual(sent["url"], "http://127.0.0.1:5000/register-node")
        self.assertEqual(sent["token"], TOKEN)
        self.assertEqual(sent["body"], {"name": agent.NODE_NAME, "ip": "127.0.0.1"})


class EnvFileTests(unittest.TestCase):
    def test_env_file_does_not_override_real_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "farm.env")
            path.write_text("# comment\nURF_TEST_A=from-file\nURF_TEST_B = \"quoted\"\nOTHER=ignored\n")
            with mock.patch.dict(os.environ, {"URF_TEST_A": "from-env"}, clear=False):
                master.load_env_file(str(path))
                self.assertEqual(os.environ["URF_TEST_A"], "from-env")
                self.assertEqual(os.environ["URF_TEST_B"], "quoted")
                self.assertNotIn("OTHER", os.environ)
            os.environ.pop("URF_TEST_B", None)



# ------------------------------------------------------------------ improvements 2026-10-06
sys.path.insert(0, str(ROOT / "agent" / "unreal"))
import urf_mrq_common as common  # noqa: E402


def result_script(result, exit_code=0):
    """Command that behaves like Unreal printing one executor result"""
    script = ("import sys, json\n"
              "print('LogPython: URF_PROGRESS ' + json.dumps({'percent': 100, 'current': 1, 'total': 1}), flush=True)\n"
              f"print('LogPython: URF_RESULT ' + json.dumps({result!r}), flush=True)\n"
              f"sys.exit({exit_code})\n")
    return lambda job, seq: [sys.executable, "-c", script]


class CameraCutPlanTests(unittest.TestCase):
    def test_boundaries_snap_to_nearby_cuts(self):
        # 0-399 in 4 pieces = boundaries 100, 200, 300; cuts near two of them
        ranges = common.plan_auto_split(0, 399, 4, 50, cuts=[95, 212, 260])
        self.assertEqual(ranges, [(0, 94), (95, 211), (212, 299), (300, 399)])
        self.assertEqual(common.cuts_used(ranges, [95, 212, 260]), 2)

    def test_far_cuts_are_ignored_and_plan_stays_contiguous(self):
        ranges = common.plan_auto_split(0, 399, 4, 50, cuts=[150, 399])
        self.assertEqual(ranges, [(0, 99), (100, 199), (200, 299), (300, 399)])

    def test_no_cuts_is_unchanged(self):
        for args in ((0, 470, 2, 50), (10, 1009, 3, 50), (0, 30, 4, 50), (0, 999, 7, 50)):
            self.assertEqual(common.plan_auto_split(*args, cuts=()), common.plan_auto_split(*args))

    def test_plan_is_deterministic_and_covers_every_frame(self):
        cuts = [0, 37, 88, 120, 201, 260, 333, 480, 512, 700]
        for pieces in range(1, 9):
            a = common.plan_auto_split(0, 749, pieces, 50, cuts)
            self.assertEqual(a, common.plan_auto_split(0, 749, pieces, 50, list(reversed(cuts))))
            self.assertEqual(a[0][0], 0)
            self.assertEqual(a[-1][1], 749)
            for (s1, e1), (s2, _) in zip(a, a[1:]):
                self.assertEqual(s2, e1 + 1)
                self.assertLessEqual(s1, e1)

    def test_prepare_frames_one_per_cut(self):
        self.assertEqual(common.prepare_frames(0, 300, [0, 120, 250, 900]), ([0, 120, 250], 3))
        frames, found = common.prepare_frames(0, 100000, range(0, 100000, 100))
        self.assertEqual((len(frames), found), (common.PREPARE_LIMIT, 1000))  # the caller warns about the rest

    def test_prepare_switch_is_read(self):
        self.assertTrue(common.task_from_params({"URFPrepare": "1"})["prepare"])


class FrameCheckTests(unittest.TestCase):
    def test_reports_missing_and_empty_files_with_frame_numbers(self):
        sizes = {"o/Shot.0101.exr": 100, "o/Shot.0102.exr": 0}

        def size_of(path):
            if path not in sizes:
                raise OSError("missing")
            return sizes[path]
        bad = common.bad_files_report(["o/Shot.0101.exr", "o/Shot.0102.exr", r"o\Shot.0103.exr"], size_of)
        self.assertEqual(bad["count"], 2)
        self.assertEqual(bad["frames"], [102, 103])

    def test_result_line_carries_bad_files(self):
        kind, data = common.parse_tagged_line(common.result_line(True, {"F": 2}, 1, 2,
                                                                 bad_files={"count": 1, "frames": [2], "examples": []}))
        self.assertEqual((kind, data["bad_files"]["count"]), ("result", 1))


class AgentImprovementTests(unittest.TestCase):
    JOB = {"job_id": "q7-1", "project": "P.uproject", "map": "/Game/M", "config": "/Game/C",
           "sequences": ["/Game/Seq/A"], "frame_start": 1, "frame_end": 2, "warmup": 0}

    def setUp(self):
        reset_agent()
        self.tmp = tempfile.TemporaryDirectory()
        for name, value in (("LOG_DIR", self.tmp.name), ("UE_MODE", "executor"),
                            ("PROJECT_ROOTS", []), ("SHARED_DDC", "")):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name, "P.uproject")
        self.project.write_text("{}")
        self.base = {"project": str(self.project), "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S"]}

    def test_ndisplay_skipped_by_default(self):
        cmd = agent.build_command(self.JOB, "/Game/Seq/A")
        self.assertIn(agent.PLAIN_GAME_ENGINE, cmd)
        with mock.patch.object(agent, "SKIP_NDISPLAY", False):
            self.assertNotIn(agent.PLAIN_GAME_ENGINE, agent.build_command(self.JOB, "/Game/Seq/A"))

    def test_shared_cache_from_job_wins(self):
        with mock.patch.object(agent, "SHARED_DDC", r"\\old\share\DDC"):
            self.assertEqual(agent.ue_environment()["UE-SharedDataCachePath"], r"\\old\share\DDC")
            env = agent.ue_environment({"shared_ddc": r"\\192.168.1.20\Cache\FarmDDC"})
        self.assertEqual(env["UE-SharedDataCachePath"], r"\\192.168.1.20\Cache\FarmDDC")
        self.assertNotIn("UE-SharedDataCachePath", agent.ue_environment({}))

    def test_shared_cache_must_be_a_network_folder(self):
        for bad in (r"D:\DDC", r"\\server", '\\\\server\\share" -ExecCmds=quit', "-ExecCmds=x", r"\\a\b" + "x" * 300):
            job, error = agent.validate_job({**self.base, "shared_ddc": bad})
            self.assertIsNone(job, bad)
            self.assertIn("shared_ddc", error)
        job, error = agent.validate_job({**self.base, "shared_ddc": r"\\192.168.1.20\Render Cache\FarmDDC"})
        self.assertIsNone(error)
        self.assertEqual(job["shared_ddc"], r"\\192.168.1.20\Render Cache\FarmDDC")

    def test_prepare_jobs(self):
        job, error = agent.validate_job({**self.base, "kind": "prepare"})
        self.assertIsNone(error)
        cmd = agent.build_command(job, "/Game/S")
        self.assertIn("-URFPrepare=1", cmd)
        self.assertFalse(any(a.startswith("-URFAutoSplit") or a.startswith("-URFStart") for a in cmd))
        job, error = agent.validate_job({"project": str(self.project), "kind": "prepare-fill"})
        self.assertIsNone(error)
        self.assertEqual(agent.build_command(job, job["sequences"][0])[1:4],
                         [str(self.project), "-run=DerivedDataCache", "-fill"])
        _, error = agent.validate_job({**self.base, "kind": "prepare", "frame_start": 1, "frame_end": 5})
        self.assertIn("prepare", error)
        _, error = agent.validate_job({**self.base, "kind": "explode"})
        self.assertIn("kind", error)

    def test_whole_project_prepare_uses_exit_code(self):
        job, _ = agent.validate_job({"project": str(self.project), "kind": "prepare-fill"})
        with mock.patch.object(agent, "build_command", fake_ue(0)):
            self.assertEqual(agent.run_sequence(job, job["sequences"][0]), "COMPLETED")
        self.assertIn("Whole project prepared", agent._results[-1]["detail"])
        with mock.patch.object(agent, "build_command", fake_ue(1)):
            self.assertEqual(agent.run_sequence(job, job["sequences"][0]), "FAILED")

    def test_missing_frames_fail_the_piece(self):
        result = {"success": True, "files_per_pass": {"FinalImage": 2}, "expected_frames": 2,
                  "frame_count_mismatch": False, "error": "", "note": "",
                  "bad_files": {"count": 2, "frames": [105, 106], "examples": []}}
        with mock.patch.object(agent, "build_command", result_script(result)):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "FAILED")
        self.assertIn("2 frame file(s) missing or empty", agent._results[-1]["detail"])
        self.assertIn("105, 106", agent._results[-1]["detail"])

    def test_count_mismatch_alone_is_only_a_warning(self):
        result = {"success": True, "files_per_pass": {"FinalImage": 3}, "expected_frames": 2,
                  "frame_count_mismatch": True, "error": "", "note": "",
                  "bad_files": {"count": 0, "frames": [], "examples": []}}
        with mock.patch.object(agent, "build_command", result_script(result)):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "COMPLETED")
        self.assertIn("check the frame-range setting", agent._results[-1]["detail"])

    def test_stall_reason(self):
        now = 10_000
        self.assertIsNone(agent.stall_reason(now, now - 60, now - 60, True, 30, 20))
        self.assertIn("froze while rendering", agent.stall_reason(now, now - 5, now - 21 * 60, True, 30, 20))
        self.assertIsNone(agent.stall_reason(now, now - 5, now - 21 * 60, False, 30, 20))  # still loading
        self.assertIn("no output for 30", agent.stall_reason(now, now - 31 * 60, now, False, 30, 20))
        self.assertIsNone(agent.stall_reason(now, now - 99999, now - 99999, True, 0, 0))  # turned off

    def test_frozen_unreal_is_killed_and_failed(self):
        script = "import time\nprint('LogInit: hello', flush=True)\ntime.sleep(60)\n"
        real_wait = threading.Event.wait

        def fast_wait(event, timeout=None):  # the watchdog checks every 15 s; don't make the test wait
            return real_wait(event, min(timeout or 0.05, 0.05))
        with mock.patch.object(agent, "build_command", lambda j, s: [sys.executable, "-c", script]), \
                mock.patch.object(agent, "LOAD_STALL_MINUTES", 0.01), \
                mock.patch.object(threading.Event, "wait", fast_wait):
            started = time.time()
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "FAILED")
        self.assertLess(time.time() - started, 20)
        self.assertIn("stopped responding", agent._results[-1]["detail"])

    def test_check_cache_folder(self):
        self.assertFalse(agent.check_cache_folder(r"C:\local")["ok"])
        with mock.patch.object(agent.os, "makedirs"), mock.patch("builtins.open", mock.mock_open()), \
                mock.patch.object(agent.os, "remove") as remove:
            self.assertTrue(agent.check_cache_folder(r"\\nas\share\DDC")["ok"])
            remove.assert_called_once()
        with mock.patch.object(agent.os, "makedirs", side_effect=PermissionError(13, "Access is denied")):
            result = agent.check_cache_folder(r"\\nas\share\DDC")
        self.assertFalse(result["ok"])
        self.assertIn("Access is denied", result["error"])


class MasterImprovementTests(QueueFixture, unittest.TestCase):
    FEATURES = ["frame_range", "auto_split", "auto_piece", "shared_ddc", "prepare", "frame_check"]

    def setUp(self):
        super().setUp()
        for n in ("A", "B"):
            self.set_stage(n, "IDLE", features=self.FEATURES)

    def test_shared_cache_setting_is_sent_with_jobs(self):
        resp = self.post("/save-settings", {"shared_ddc": ' "\\\\192.168.1.20\\Cache\\FarmDDC\\" '})
        self.assertEqual(resp.get_json()["shared_ddc"], r"\\192.168.1.20\Cache\FarmDDC")
        self.assertEqual(self.client.get("/get-settings", headers=self.auth).get_json()["shared_ddc"],
                         r"\\192.168.1.20\Cache\FarmDDC")
        self.queue(["/Game/S1"])
        master.schedule_jobs()
        self.assertEqual(self.sent[0][1]["shared_ddc"], r"\\192.168.1.20\Cache\FarmDDC")

    def test_shared_cache_rejects_drive_letters(self):
        resp = self.post("/save-settings", {"shared_ddc": r"N:\DDC"})
        self.assertEqual(resp.status_code, 400)
        self.post("/save-settings", {"shared_ddc": ""})
        self.queue(["/Game/S1"])
        master.schedule_jobs()
        self.assertNotIn("shared_ddc", self.sent[0][1])

    def test_check_cache_asks_every_computer(self):
        self.set_stage("B", "OFFLINE")
        self.responses["A"] = mock.Mock(ok=True, status_code=200, json=lambda: {"ok": True, "error": ""})
        data = self.post("/check-cache", {"path": r"\\nas\share\DDC"}).get_json()
        self.assertEqual(data["results"], [{"node": "A", "ok": True, "error": ""},
                                           {"node": "B", "ok": False, "error": "offline"}])
        self.assertEqual(self.post("/check-cache", {}).status_code, 400)  # nothing saved yet

    def test_quick_prepare_is_rush_and_never_split(self):
        self.queue(["/Game/Low"], priority=2)
        data = self.queue(["/Game/S1", "/Game/S2"], prepare="quick", auto_split=True)
        self.assertEqual((data["jobs"], data["prepare"]), (2, "quick"))
        master.schedule_jobs()
        payloads = [p for _, p in self.sent]
        self.assertEqual([p.get("kind") for p in payloads], ["prepare", "prepare"])
        self.assertTrue(all("auto_split" not in p and "frame_start" not in p for p in payloads))

    def test_prepare_goes_only_to_agents_that_can(self):
        self.set_stage("A", "IDLE", features=["frame_range", "auto_split", "auto_piece"])
        self.queue(["/Game/S1"], prepare="quick")
        master.schedule_jobs()
        self.assertEqual([n for n, _ in self.sent], ["B"])

    def test_whole_project_prepare_needs_only_the_project(self):
        data = self.post("/launch", {"project": "C:/P.uproject", "sequences": ["WholeProject"],
                                     "prepare": "whole"}).get_json()
        self.assertEqual(data["jobs"], 1)
        master.schedule_jobs()
        self.assertEqual(self.sent[0][1]["kind"], "prepare-fill")
        self.assertEqual(self.post("/launch", {**self.BATCH, "sequences": ["/Game/S"], "prepare": "x"}).status_code, 400)

    def test_shot_shows_frames_written(self):
        self.queue([{"path": "/Game/Long", "frames": "0-199"}], chunk_size=100)
        master.schedule_jobs()
        for seq_no, (node, payload) in enumerate(self.sent, 1):
            master.record_results(node, f"boot-{node}", [{"seq": seq_no, "job_id": payload["job_id"], "project": "P",
                                                          "sequence": "/Game/Long", "status": "COMPLETED",
                                                          "frames": 100, "detail": ""}])
        shot = self.client.get("/get-queue", headers=self.auth).get_json()["shots"][0]
        self.assertEqual((shot["frames_written"], shot["frames_expected"]), (200, 200))


# ------------------------------------------------------------------ review fixes 2026-10-06
class SplitReviewTests(unittest.TestCase):
    def test_no_piece_shorter_than_min_chunk(self):
        self.assertEqual(common.plan_auto_split(0, 9, 3, 3), [(0, 3), (4, 6), (7, 9)])  # was 4, 4, 2
        rng = random.Random(7)
        for _ in range(3000):
            start = rng.randint(0, 500)
            total = rng.randint(1, 2000)
            end = start + total - 1
            min_chunk = rng.randint(1, 120)
            pieces = rng.randint(1, 12)
            cuts = sorted(rng.sample(range(start, end + 1), min(total, rng.randint(0, 40))))
            plan = common.plan_auto_split(start, end, pieces, min_chunk, cuts)
            self.assertEqual((plan[0][0], plan[-1][1]), (start, end))
            for (a1, b1), (a2, _) in zip(plan, plan[1:]):
                self.assertEqual(a2, b1 + 1)
            if total >= min_chunk:
                self.assertTrue(all(b - a + 1 >= min_chunk for a, b in plan), (start, end, pieces, min_chunk, cuts, plan))
            self.assertLessEqual(len(plan), pieces)

    def test_snap_never_makes_a_short_piece(self):
        # a cut right at the edge of the allowed reach would leave piece 1 under 90 frames: not taken
        plan = common.plan_auto_split(0, 199, 2, 90, cuts=[76])
        self.assertEqual(plan, [(0, 99), (100, 199)])

    def test_frames_seen_late_on_the_nas_are_not_bad(self):
        seen = {"a.0001.exr": [0, 5], "a.0002.exr": [0, 0, 0, 0], "a.0003.exr": [7]}
        waits = []

        def size_of(path):
            sizes = seen[path]
            return sizes.pop(0) if len(sizes) > 1 else sizes[0]
        bad = common.still_bad_files(list(seen), size_of, tries=3, wait=2, sleep=waits.append)
        self.assertEqual(bad, ["a.0002.exr"])
        self.assertEqual(waits, [2, 2, 2])
        self.assertEqual(common.still_bad_files(["a.0003.exr"], size_of, sleep=waits.append), [])


class WatchdogReviewTests(unittest.TestCase):
    JOB = {"job_id": "q7-1", "project": "P.uproject", "map": "/Game/M", "config": "/Game/C",
           "sequences": ["/Game/Seq/A"], "warmup": 0}

    def setUp(self):
        reset_agent()
        self.tmp = tempfile.TemporaryDirectory()
        for name, value in (("LOG_DIR", self.tmp.name), ("UE_MODE", "executor")):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        real_wait = threading.Event.wait
        patcher = mock.patch.object(threading.Event, "wait", lambda e, t=None: real_wait(e, min(t or 0.05, 0.05)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ue(self, body):
        script = "import sys, json, time\n" + body
        return lambda job, seq: [sys.executable, "-c", script]

    def test_watchdog_survives_errors(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None, None, 0]
        clock = {"line": 0, "progress": 0, "rendering": True, "frozen": None}
        with mock.patch.object(agent, "stall_reason", side_effect=[psutil_error(), "frozen!"]), \
                mock.patch.object(agent, "kill_process_tree") as kill:
            agent.watch_for_freeze(proc, clock, threading.Event())
        self.assertEqual(clock["frozen"], "frozen!")
        kill.assert_called_once_with(proc)

    def test_slow_frame_with_heartbeats_is_not_killed(self):
        # The percentage never moves for ~1.5 s, but heartbeats keep coming: a slow frame, not a freeze
        body = ("for i in range(15):\n"
                "    print('LogPython: URF_PROGRESS ' + json.dumps({'percent': 10, 'current': 1, 'total': 10}), flush=True)\n"
                "    time.sleep(0.1)\n"
                "print('LogPython: URF_RESULT ' + json.dumps({'success': True, 'files_per_pass': {}, 'error': ''}), flush=True)\n")
        with mock.patch.object(agent, "build_command", self.ue(body)), \
                mock.patch.object(agent, "RENDER_STALL_MINUTES", 0.01):
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "COMPLETED")

    def test_engine_that_stops_ticking_is_killed(self):
        # Unreal keeps writing log lines but the executor's heartbeat stops: the render is stuck
        body = ("print('LogPython: URF_PROGRESS ' + json.dumps({'percent': 10}), flush=True)\n"
                "for i in range(300):\n    print('LogRenderer: busy', flush=True); time.sleep(0.1)\n")
        with mock.patch.object(agent, "build_command", self.ue(body)), \
                mock.patch.object(agent, "RENDER_STALL_MINUTES", 0.01):
            started = time.time()
            self.assertEqual(agent.run_sequence(self.JOB, "/Game/Seq/A"), "FAILED")
        self.assertLess(time.time() - started, 20)
        self.assertIn("froze while rendering", agent._results[-1]["detail"])

    def test_whole_project_fill_gets_the_long_limit(self):
        with mock.patch.object(agent, "watch_for_freeze") as watch, \
                mock.patch.object(agent, "build_command", fake_ue(0)):
            agent.run_sequence({**self.JOB, "kind": "prepare-fill"}, "WholeProject")
        self.assertEqual(watch.call_args[0][3], agent.FILL_STALL_MINUTES)

    @unittest.skipUnless(os.name == "nt", "job objects are Windows only")
    def test_kill_takes_children_started_late(self):
        body = ("import subprocess\n"
                "time.sleep(0.5)\n"  # the child starts after Unreal does, like ShaderCompileWorker
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "print(c.pid, flush=True)\ntime.sleep(60)\n")
        proc = agent.subprocess.Popen(self.ue(body)(None, None), stdout=agent.subprocess.PIPE, text=True)
        proc.farm_job = agent.ProcessJob(proc)
        self.assertIsNotNone(proc.farm_job.handle)
        child = int(proc.stdout.readline())
        agent.kill_process_tree(proc)
        proc.wait(10)
        deadline = time.time() + 5
        while agent.psutil.pid_exists(child) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(agent.psutil.pid_exists(child))
        proc.farm_job.close()


def psutil_error():
    return agent.psutil.AccessDenied(1)


class PlanMismatchTests(QueueFixture, unittest.TestCase):
    PLAN = {"start": 0, "end": 599, "ranges": [[0, 299], [300, 599]], "note": ""}

    def test_piece_with_a_different_plan_is_rendered_again_with_the_shots_frames(self):
        self.queue(["/Game/Long"], auto_split=True)
        master.schedule_jobs(); master.schedule_jobs()                 # A: piece 1 (q1-1), B: piece 2 (q2-1)
        master.expand_auto_job("q1-1", {**self.PLAN, "index": 1, "job_id": "q1-1"})
        self.set_stage("B", "RENDERING", job_id="q2-1")
        stale = {"start": 0, "end": 599, "ranges": [[0, 349], [350, 599]], "index": 2, "job_id": "q2-1", "note": ""}
        master.expand_auto_job("q2-1", stale)
        job = self.jobs()[2]
        self.assertEqual(job["status"], "QUEUED")
        self.assertIn("planned frames 350-599", job["detail"])
        self.assertTrue(any(c.args[0].endswith("/cancel") for c in master.HTTP.post.call_args_list))
        # its success for the wrong frames does not count
        master.record_results("B", "boot-B", [{"seq": 1, "job_id": "q2-1", "project": "P", "sequence": "/Game/Long",
                                               "status": "COMPLETED", "frame_start": 350, "frame_end": 599}])
        self.assertEqual(self.jobs()[2]["status"], "QUEUED")
        self.set_stage("B", "IDLE")
        master.schedule_jobs()
        retry = self.sent[-1][1]
        self.assertEqual((retry["frame_start"], retry["frame_end"], retry["job_id"]), (300, 599, "q2-2"))

    def test_matching_plans_change_nothing(self):
        self.queue(["/Game/Long"], auto_split=True)
        master.schedule_jobs(); master.schedule_jobs()
        master.expand_auto_job("q1-1", {**self.PLAN, "index": 1, "job_id": "q1-1"})
        master.expand_auto_job("q2-1", {**self.PLAN, "index": 2, "job_id": "q2-1"})
        self.assertEqual([j["status"] for j in self.jobs().values()], ["ASSIGNED", "ASSIGNED"])


class MasterStorageReviewTests(QueueFixture, unittest.TestCase):
    def test_database_uses_wal(self):
        with master.connect_db() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_poll_of_an_old_address_does_not_overwrite_the_new_one(self):
        old_info = dict(master.RENDER_NODES["A"])
        with master.NODES_LOCK:
            master.RENDER_NODES["A"] = {"ip": "192.168.1.99"}
        self.set_stage("A", "IDLE")
        with mock.patch.object(master.HTTP, "get", side_effect=master.requests.RequestException("down")):
            master.poll_node("A", old_info)
        self.assertEqual(master.STATUS_CACHE["A"]["stage"], "IDLE")


class FakeUnreal:
    """Just enough of Unreal's Python API to run urf_executor.py outside Unreal, with the behaviour
    that bit us on 2026-10-05: every call from Unreal gets a NEW Python wrapper of the same object, so
    only uproperty fields survive between calls, and _post_init is never run."""

    def __init__(self, params, playback=(0, 100), cuts=(), fail_finish=False, outputs=(), preset_dir="",
                 project_dir="//nas/share/Film"):
        import types as _types
        self.params, self.playback, self.cuts = params, playback, list(cuts)
        self.outputs, self.preset_dir, self.project_dir = list(outputs), preset_dir, project_dir
        self.lines, self.pipelines, self.finished = [], [], 0
        self.storage = {}  # the Unreal object's uproperty values
        fake = self
        u = _types.ModuleType("unreal")
        self.module = u
        u.log = lambda m: fake.lines.append(str(m))
        u.log_warning = lambda m: fake.lines.append("WARNING " + str(m))
        u.log_error = lambda m: fake.lines.append("ERROR " + str(m))
        def uclass():
            def generate(cls):
                for name, value in vars(cls).items():
                    if isinstance(value, UProperty) and any(hasattr(base, name) for base in cls.__mro__[1:]):
                        raise Exception(f"{cls.__name__}: Property '{name}' cannot override a property from the base type")
                return cls
            return generate
        u.uclass = uclass
        u.ufunction = lambda **kw: (lambda f: f)

        class UProperty:
            def __init__(self, _type):
                self.name = None

            def __set_name__(self, owner, name):
                self.name = name

            def __get__(self, obj, owner=None):
                return self if obj is None else fake.storage.get(self.name)

            def __set__(self, obj, value):
                fake.storage[self.name] = value
        u.uproperty = UProperty

        class Named:
            def get_class(self):
                return _types.SimpleNamespace(get_name=lambda: type(self).__name__)

        class MoviePipelineOutputSetting(Named):
            def __init__(self):
                self.use_custom_playback_range = False
                self.custom_start_frame = self.custom_end_frame = 0
                self.output_directory = DirectoryPath(fake.preset_dir)

        class MoviePipelineAntiAliasingSetting(Named):
            engine_warm_up_count = render_warm_up_count = 0

        class DirectoryPath:
            def __init__(self, path=""):
                self.path = path

        class Config(Named):
            def __init__(self):
                self.settings = {}

            def find_or_add_setting_by_class(self, cls):
                return self.settings.setdefault(cls, cls())

            def remove_setting(self, setting):
                self.settings = {k: v for k, v in self.settings.items() if v is not setting}

            def get_all_settings(self):
                return list(self.settings.values())

            def initialize_transient_settings(self):
                pass

        class MoviePipelinePrimaryConfig(Config):
            pass

        class Job:
            def set_configuration(self, preset):
                self.config = MoviePipelinePrimaryConfig()
                for name in fake.outputs:  # the preset's output settings, e.g. MoviePipelineMP4EncoderOutput
                    self.config.find_or_add_setting_by_class(output_classes.setdefault(name, type(name, (Named,), {})))

            def get_configuration(self):
                return self.config

        class MoviePipelineQueue:
            def __init__(self):
                self.jobs = []

            def allocate_new_job(self, cls):
                self.jobs.append(Job())
                return self.jobs[-1]

            def delete_job(self, job):
                self.jobs.remove(job)

        class Delegate:
            def add_function_unique(self, obj, name):
                self.name = name

        class Pipeline:
            def __init__(self):
                self.on_movie_pipeline_work_finished_delegate = Delegate()
                self.percent = 0.0

            def initialize(self, job):
                self.job = job
                fake.pipelines.append(self)

        class Section:
            def __init__(self, frame):
                self.frame = frame

            def is_active(self):
                return True

            def has_start_frame(self):
                return True

            def get_start_frame(self):
                return self.frame

        class Track:
            def __init__(self, frames):
                self.sections = [Section(f) for f in frames]

            def get_sections(self):
                return self.sections

        class Sequence:
            def get_playback_start(self):
                return fake.playback[0]

            def get_playback_end(self):
                return fake.playback[1]

            def find_tracks_by_type(self, cls):
                return [Track(fake.cuts)] if fake.cuts and cls is u.MovieSceneCameraCutTrack else []

        class Host:
            target_pipeline_class = Pipeline
            pipeline_queue = None  # Unreal's own property on MoviePipelineExecutorBase

            def on_begin_frame(self):
                pass

            def on_executor_finished_impl(self):
                fake.finished += 1

            def get_last_loaded_world(self):
                return _types.SimpleNamespace(get_path_name=lambda: "/Game/Map.Map")

        def new_object(cls, outer=None, base_type=None):
            return cls()

        def load_asset(path):
            return Sequence() if path == fake.params["LevelSequence"] else MoviePipelinePrimaryConfig()

        u.MoviePipelinePythonHostExecutor = Host
        u.MoviePipeline = Pipeline
        u.MoviePipelineQueue = MoviePipelineQueue
        u.MoviePipelineExecutorJob = Job
        u.MoviePipelineOutputData = object
        u.MoviePipelineOutputSetting = MoviePipelineOutputSetting
        u.MoviePipelineAntiAliasingSetting = MoviePipelineAntiAliasingSetting
        u.MovieSceneCameraCutTrack = type("MovieSceneCameraCutTrack", (), {})
        output_classes = {}
        u.SoftObjectPath = lambda value: value
        u.DirectoryPath = DirectoryPath
        u.Paths = _types.SimpleNamespace(project_dir=lambda: fake.project_dir,
                                         convert_relative_path_to_full=lambda p: p)
        u.new_object = new_object
        u.load_asset = load_asset
        u.SystemLibrary = _types.SimpleNamespace(get_command_line=lambda: "",
                                                 parse_command_line=lambda _: ([], [], dict(fake.params)))
        u.MoviePipelineLibrary = _types.SimpleNamespace(
            get_completion_percentage=lambda p: p.percent,
            get_overall_output_frames=lambda p: (int(p.percent * 10), 10))
        self.fail_finish = fail_finish

    def load(self):
        import importlib
        sys.modules["unreal"] = self.module
        self.addCleanup = lambda: sys.modules.pop("unreal", None)
        sys.modules.pop("urf_executor", None)
        self.executor_module = importlib.import_module("urf_executor")
        return self

    def call(self, method, *args):
        """Unreal calling into the executor: a brand-new wrapper every time, __init__/_post_init not run"""
        cls = self.executor_module.URFExecutor
        return getattr(cls.__new__(cls), method)(*args)

    def results(self, files, success=True):
        import types as _types
        data = _types.SimpleNamespace(file_paths=files)
        shot = _types.SimpleNamespace(render_pass_data={type("PassId", (), {"name": "FinalImage"})(): data})
        return _types.SimpleNamespace(success=success, shot_data=[shot])

    def tagged(self, kind):
        return [common.parse_tagged_line(line)[1] for line in self.lines
                if common.parse_tagged_line(line) and common.parse_tagged_line(line)[0] == kind]


class ExecutorInFakeUnrealTests(unittest.TestCase):
    PARAMS = {"LevelSequence": "/Game/Seq/Shot", "MoviePipelineConfig": "/Game/Cfg", "URFJob": "q1-1"}

    def tearDown(self):
        sys.modules.pop("unreal", None)
        sys.modules.pop("urf_executor", None)

    def frames(self, n, empty=()):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        paths = []
        for i in range(n):
            p = Path(tmp, f"Shot.{i:04d}.jpeg")
            p.write_bytes(b"" if i in empty else b"jpeg")
            paths.append(str(p))
        return paths

    def test_a_frame_range_render_reports_progress_and_result_and_lets_unreal_exit(self):
        ue = FakeUnreal({**self.PARAMS, "URFStart": "10", "URFEnd": "19", "URFWarmup": "8"}).load()
        ue.call("execute_delayed", None)
        self.assertEqual(len(ue.pipelines), 1, ue.lines)
        output = ue.pipelines[0].job.config.settings[ue.module.MoviePipelineOutputSetting]
        self.assertEqual((output.custom_start_frame, output.custom_end_frame), (10, 20))
        for percent in (0.0, 0.25, 0.5, 1.0):
            ue.pipelines[0].percent = percent
            ue.call("on_begin_frame")
        self.assertGreaterEqual(len(ue.tagged("progress")), 3)
        self.assertFalse([l for l in ue.lines if l.startswith(("ERROR", "WARNING"))], ue.lines)
        ue.call("on_movie_pipeline_finished", ue.results(self.frames(10)))
        result = ue.tagged("result")[-1]
        self.assertTrue(result["success"], result)
        self.assertEqual((result["files_per_pass"], result["expected_frames"], result["bad_files"]["count"]),
                         ({"FinalImage": 10}, 10, 0))
        self.assertEqual(ue.finished, 1)

    def test_heartbeat_while_the_percentage_stands_still(self):
        ue = FakeUnreal(dict(self.PARAMS)).load()
        ue.call("execute_delayed", None)
        ue.call("on_begin_frame")
        with mock.patch.object(ue.executor_module.time, "time", return_value=time.time() + 31):
            ue.call("on_begin_frame")
        self.assertEqual(len(ue.tagged("progress")), 2)

    def test_automatic_piece_uses_the_shared_plan(self):
        ue = FakeUnreal({**self.PARAMS, "URFAutoSplit": "2", "URFAutoIndex": "2", "URFMinChunk": "50"},
                        playback=(0, 471)).load()
        ue.call("execute_delayed", None)
        plan = ue.tagged("range")[-1]
        self.assertEqual(plan["ranges"], [[0, 235], [236, 470]])
        output = ue.pipelines[0].job.config.settings[ue.module.MoviePipelineOutputSetting]
        self.assertEqual((output.custom_start_frame, output.custom_end_frame), (236, 471))

    def test_missing_frames_are_reported(self):
        ue = FakeUnreal({**self.PARAMS, "URFStart": "0", "URFEnd": "3"}).load()
        ue.call("execute_delayed", None)
        with mock.patch.object(ue.executor_module, "still_bad_files",
                               side_effect=lambda paths, size_of: [p for p in paths if not common.file_ok(p, size_of)]):
            ue.call("on_movie_pipeline_finished", ue.results(self.frames(4, empty={2})))
        self.assertEqual(ue.tagged("result")[-1]["bad_files"]["frames"], [2])
        self.assertEqual(ue.finished, 1)

    def test_prepare_renders_one_frame_per_cut_one_after_another(self):
        ue = FakeUnreal({**self.PARAMS, "URFPrepare": "1"}, playback=(0, 120), cuts=[0, 40, 80]).load()
        ue.call("execute_delayed", None)
        for _ in range(3):
            pipeline = ue.pipelines[-1]
            pipeline.percent = 1.0
            ue.call("on_begin_frame")
            ue.call("on_movie_pipeline_finished", ue.results([]))
        starts = [p.job.config.settings[ue.module.MoviePipelineOutputSetting].custom_start_frame for p in ue.pipelines]
        self.assertEqual(starts, [0, 40, 80])
        result = ue.tagged("result")[-1]
        self.assertTrue(result["success"])
        self.assertIn("Prepared 3 camera cut", result["note"])
        self.assertEqual(ue.finished, 1)

    def test_an_error_after_the_render_still_reports_and_exits(self):
        ue = FakeUnreal({**self.PARAMS, "URFStart": "0", "URFEnd": "3"}).load()
        ue.call("execute_delayed", None)
        with mock.patch.object(ue.executor_module, "bad_files_report", side_effect=RuntimeError("disk gone")):
            ue.call("on_movie_pipeline_finished", ue.results(self.frames(4)))
        result = ue.tagged("result")[-1]
        self.assertFalse(result["success"])
        self.assertIn("disk gone", result["error"])
        self.assertEqual(ue.finished, 1)

    def test_executor_keeps_no_plain_state_on_self(self):
        import ast
        tree = ast.parse((ROOT / "agent" / "unreal" / "urf_executor.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "URFExecutor")
        methods = {f.name for f in cls.body if isinstance(f, ast.FunctionDef)}
        uprops = {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        base_api = {"target_pipeline_class", "on_executor_finished_impl", "get_last_loaded_world"}
        self.assertNotIn("pipeline_queue", uprops, "the base class already has pipeline_queue")
        used = {n.attr for n in ast.walk(cls) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "self"}
        self.assertEqual(used - methods - uprops - base_api, set(),
                         "plain attributes on self are lost between Unreal's calls: use RUN or a uproperty")
        reset = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "_reset")
        set_in_reset = {t.attr for n in ast.walk(reset) if isinstance(n, ast.Assign) for t in n.targets
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "RUN"}
        run_used = {n.attr for n in ast.walk(cls) if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name) and n.value.id == "RUN"}
        self.assertEqual(run_used - set_in_reset, set())


class ScriptTroubleTests(unittest.TestCase):
    def test_repeated_script_errors_and_silent_finish_stop_unreal(self):
        now = 1000
        self.assertIsNone(agent.script_trouble({}, now))
        self.assertIn("kept failing", agent.script_trouble(
            {"script_errors": 25, "script_error": "AttributeError: x"}, now))
        self.assertIsNone(agent.script_trouble({"completed_at": now - 60}, now))
        self.assertIn("did not report back", agent.script_trouble({"completed_at": now - 200}, now))
        self.assertIsNone(agent.script_trouble({"completed_at": now - 200, "result": True}, now))

    def test_render_hung_after_finishing_is_stopped(self):
        reset_agent()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        body = ("import time\nprint('LogMovieRenderPipeline: Movie Pipeline completed. Duration: +00:17:13', flush=True)\n"
                "for i in range(600):\n"
                "    print('LogScript: Error: Script Msg: Traceback (most recent call last):', flush=True)\n"
                "    print(\"AttributeError: 'URFExecutor' object has no attribute 'prepare_dir'\", flush=True)\n"
                "    time.sleep(0.05)\n")
        real_wait = threading.Event.wait
        with mock.patch.object(agent, "LOG_DIR", tmp.name), mock.patch.object(agent, "UE_MODE", "executor"), \
                mock.patch.object(agent, "build_command", lambda j, s: [sys.executable, "-c", body]), \
                mock.patch.object(threading.Event, "wait", lambda e, t=None: real_wait(e, min(t or 0.05, 0.05))):
            job = {"job_id": "q1-1", "project": "P", "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S"]}
            started = time.time()
            self.assertEqual(agent.run_sequence(job, "/Game/S"), "FAILED")
        self.assertLess(time.time() - started, 25)
        detail = agent._results[-1]["detail"]
        self.assertIn("kept failing", detail)
        self.assertIn("no attribute 'prepare_dir'", detail)


class TypedFramesSplitTests(QueueFixture, unittest.TestCase):
    def test_short_typed_range_is_not_cut_into_a_sliver(self):
        # 2026-10-05: 0-55 on 2 computers became 0-49 + 50-55 (6 frames paying a whole Unreal start-up)
        data = self.queue([{"path": "/Game/S", "frames": "0-55"}], auto_split=True, nodes=["A", "B"])
        self.assertEqual(data["jobs"], 1)
        master.schedule_jobs()
        self.assertEqual([(p["frame_start"], p["frame_end"]) for _, p in self.sent], [(0, 55)])

    def test_typed_range_splits_evenly(self):
        self.queue([{"path": "/Game/S", "frames": "0-199"}], auto_split=True, nodes=["A", "B"])
        master.schedule_jobs()
        self.assertEqual(sorted((p["frame_start"], p["frame_end"]) for _, p in self.sent), [(0, 99), (100, 199)])

    def test_even_pieces(self):
        self.assertEqual(master.even_pieces(0, 9, 3, 3), [(0, 3), (4, 6), (7, 9)])
        self.assertEqual(master.even_pieces(5, 20, 4, 50), [(5, 20)])


class ProgressFileTests(unittest.TestCase):
    """2026-10-05: Unreal's piped stdout is buffered, so the card showed 'loading' for a whole render"""

    def setUp(self):
        reset_agent()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("LOG_DIR", self.tmp.name), ("UE_MODE", "executor")):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_executor_writes_its_progress_to_the_file(self):
        path = str(Path(self.tmp.name, "q1-1.progress"))
        ue = FakeUnreal({**ExecutorInFakeUnrealTests.PARAMS, "URFStatusFile": path}).load()
        self.addCleanup(lambda: (sys.modules.pop("unreal", None), sys.modules.pop("urf_executor", None)))
        ue.call("execute_delayed", None)
        ue.pipelines[0].percent = 0.4
        ue.call("on_begin_frame")
        kind, data = common.parse_tagged_line(Path(path).read_text(encoding="utf-8"))
        self.assertEqual((kind, data["percent"], data["current"]), ("progress", 40.0, 4))

    def test_card_shows_progress_while_stdout_is_silent(self):
        job = {"job_id": "q1-1", "project": "P", "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S"]}
        self.assertIn(f"-URFStatusFile={agent.progress_file_for(job)}", agent.build_command(job, "/Game/S"))
        script = ("import sys, json, time\n"
                  "path = [a.split('=', 1)[1] for a in sys.argv if a.startswith('-URFStatusFile=')][0]\n"
                  "open(path, 'w').write('URF_PROGRESS ' + json.dumps({'percent': 40.0, 'current': 4, 'total': 10}))\n"
                  "time.sleep(5)\n"  # nothing on stdout meanwhile, like Unreal's buffered pipe
                  "print('LogPython: URF_PROGRESS ' + json.dumps({'percent': 10.0, 'current': 1, 'total': 10}), flush=True)\n"
                  "print('LogPython: URF_RESULT ' + json.dumps({'success': True, 'files_per_pass': {}, 'error': ''}), flush=True)\n")
        real = agent.build_command
        with mock.patch.object(agent, "build_command",
                               lambda j, s: [sys.executable, "-c", script] + [a for a in real(j, s) if a.startswith("-")]):
            worker = threading.Thread(target=agent.run_sequence, args=(job, "/Game/S"))
            worker.start()
            seen = 0
            deadline = time.time() + 4.5
            while time.time() < deadline and not seen:
                seen = agent.CURRENT_STATUS["progress"]
                time.sleep(0.1)
            worker.join(15)
        self.assertEqual(seen, 40.0)
        self.assertEqual(agent._results[-1]["status"], "COMPLETED")
        self.assertFalse(Path(agent.progress_file_for(job)).exists())  # tidied up

    def test_late_old_line_does_not_move_the_bar_back(self):
        agent.update_status(stage="RENDERING", start_time=time.time() - 10)
        agent.parse_ue_output('URF_PROGRESS {"percent": 50.0, "current": 5, "total": 10}')
        agent.parse_ue_output('LogPython: URF_PROGRESS {"percent": 20.0, "current": 2, "total": 10}')
        self.assertEqual((agent.CURRENT_STATUS["progress"], agent.CURRENT_STATUS["current_frame"]), (50.0, 5))


class OutputFolderTests(unittest.TestCase):
    """Frames always land where the whole farm can see them (2026-10-06: a preset saved to E: on the
    rendering PC), and a split piece keeps its pictures but skips the preset's video"""
    PARAMS = {"LevelSequence": "/Game/Seq/Shot", "MoviePipelineConfig": "/Game/Cfg", "URFJob": "q1-1"}

    def tearDown(self):
        sys.modules.pop("unreal", None)
        sys.modules.pop("urf_executor", None)
        os.environ.pop("URF_OUTPUT_DIR", None)

    def kinds(self, mapping):
        return lambda path: next((v for k, v in mapping.items() if str(path).startswith(k)), "unknown")

    def run_job(self, ue, kinds):
        with mock.patch.object(ue.executor_module, "drive_kind", kinds):
            ue.call("execute_delayed", None)
        job = ue.pipelines[-1].job
        output = job.config.settings[ue.module.MoviePipelineOutputSetting]
        return output, [type(v).__name__ for v in job.config.settings.values()]

    def test_choose_output_dir(self):
        kinds = self.kinds({"E:": "local", "K:": "network", "//": "network", "D:": "local"})
        self.assertEqual(common.choose_output_dir("K:\\Renders\\Film\\{sequence_name}", "E:/x", "K:/Film", kinds)[0],
                         "K:\\Renders\\Film\\{sequence_name}")
        target, note = common.choose_output_dir("", "E:/MyShots/Renders", "K:/Studio/Film", kinds)
        self.assertEqual(target, "K:/Studio/Film/Renders/{sequence_name}")
        self.assertIn("E:/MyShots/Renders", note)
        self.assertEqual(common.choose_output_dir("", "K:/Out", "K:/Film", kinds), ("", ""))
        self.assertEqual(common.choose_output_dir("", "{project_dir}/Saved/MovieRenders", "K:/Film", kinds), ("", ""))
        self.assertEqual(common.choose_output_dir("", "E:/x", "D:/LocalProject", kinds), ("", ""))  # one-PC setup

    def test_drive_kind(self):
        self.assertEqual(common.drive_kind(r"\\nas\share\x"), "network")
        self.assertEqual(common.drive_kind("K:/x", get_drive_type=lambda root: 4), "network")
        self.assertEqual(common.drive_kind("E:/x", get_drive_type=lambda root: 3), "local")
        self.assertEqual(common.drive_kind("relative/x"), "unknown")

    def test_farm_folder_wins_over_the_preset(self):
        os.environ["URF_OUTPUT_DIR"] = "K:\\Renders\\Film\\{sequence_name}"
        ue = FakeUnreal(dict(self.PARAMS), preset_dir="E:/MyShots/Renders").load()
        output, _ = self.run_job(ue, self.kinds({}))
        self.assertEqual(output.output_directory.path, "K:\\Renders\\Film\\{sequence_name}")
        ue.call("on_movie_pipeline_finished", ue.results([]))
        self.assertIn("Frames saved to K:\\Renders\\Film", ue.tagged("result")[-1]["note"])

    def test_local_preset_folder_goes_to_the_shared_drive(self):
        ue = FakeUnreal(dict(self.PARAMS), preset_dir="E:/MyShots/Renders", project_dir="K:/Studio/Film").load()
        output, _ = self.run_job(ue, self.kinds({"E:": "local", "K:": "network"}))
        self.assertEqual(output.output_directory.path, "K:/Studio/Film/Renders/{sequence_name}")

    def test_shared_preset_folder_is_kept(self):
        ue = FakeUnreal(dict(self.PARAMS), preset_dir="K:/Out", project_dir="K:/Film").load()
        output, _ = self.run_job(ue, self.kinds({"K:": "network"}))
        self.assertEqual(output.output_directory.path, "K:/Out")

    def test_split_piece_keeps_pictures_and_skips_mp4(self):
        outputs = ["MoviePipelineImageSequenceOutput_JPG", "MoviePipelineImageSequenceOutput_PNG",
                   "MoviePipelineMP4EncoderOutput"]
        ue = FakeUnreal({**self.PARAMS, "URFAutoSplit": "2", "URFAutoIndex": "1", "URFMinChunk": "50"},
                        playback=(0, 300), outputs=outputs).load()
        output, names = self.run_job(ue, self.kinds({}))
        self.assertEqual(ue.tagged("range")[-1]["ranges"], [[0, 149], [150, 299]])  # shared, not whole
        self.assertIn("MoviePipelineImageSequenceOutput_JPG", names)
        self.assertNotIn("MoviePipelineMP4EncoderOutput", names)
        ue.call("on_movie_pipeline_finished", ue.results([]))
        self.assertIn("MP4EncoderOutput skipped", ue.tagged("result")[-1]["note"])

    def test_video_only_preset_still_renders_whole(self):
        ue = FakeUnreal({**self.PARAMS, "URFAutoSplit": "2", "URFAutoIndex": "1", "URFMinChunk": "50"},
                        playback=(0, 300), outputs=["MoviePipelineMP4EncoderOutput"]).load()
        _, names = self.run_job(ue, self.kinds({}))
        self.assertEqual(ue.tagged("range")[-1]["ranges"], [[0, 299]])
        self.assertIn("MoviePipelineMP4EncoderOutput", names)


class OutputFolderAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        project = Path(self.tmp.name, "P.uproject")
        project.write_text("{}")
        self.base = {"project": str(project), "map": "/Game/M", "config": "/Game/C", "sequences": ["/Game/S"]}
        for name, value in (("UE_MODE", "executor"), ("PROJECT_ROOTS", [])):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_output_dir_is_validated_and_passed_to_unreal(self):
        job, error = agent.validate_job({**self.base, "output_dir": "K:\\Renders\\Film\\{sequence_name}"})
        self.assertIsNone(error)
        self.assertEqual(agent.ue_environment(job)["URF_OUTPUT_DIR"], "K:\\Renders\\Film\\{sequence_name}")
        self.assertNotIn("URF_OUTPUT_DIR", agent.ue_environment({}))
        for bad in ("Renders", '"K:\\x"', "-ExecCmds=quit", "K:\\a|b"):
            self.assertIn("output_dir", agent.validate_job({**self.base, "output_dir": bad})[1])

    def test_out_of_memory_is_flagged(self):
        agent._results.clear()
        result = agent.record_result({"job_id": "q1-1", "project": "P"}, "/Game/S", "FAILED", time.time(),
                                     detail="Unreal said: Fatal error: Ran out of memory allocating 6284986680 bytes")
        self.assertTrue(result["out_of_memory"])


class OutputFolderMasterTests(QueueFixture, unittest.TestCase):
    def test_farm_output_folder_is_sent_per_project(self):
        self.assertEqual(self.post("/save-settings", {"output_root": "K:/Renders/"}).get_json()["output_root"],
                         "K:\\Renders")
        self.post("/launch", {**self.BATCH, "project": "C:/Projects/Car_Spot.uproject",
                              "sequences": ["/Game/S1"], "auto_split": False})
        master.schedule_jobs()
        self.assertEqual(self.sent[0][1]["output_dir"], "K:\\Renders\\Car_Spot\\{sequence_name}")

    def test_render_folder_beats_the_farm_folder(self):
        self.post("/save-settings", {"output_root": "K:\\Renders"})
        self.queue(["/Game/S1"], output_dir="\\\\nas\\jobs\\Shot1")
        master.schedule_jobs()
        self.assertEqual(self.sent[0][1]["output_dir"], "\\\\nas\\jobs\\Shot1")

    def test_no_folder_set_keeps_the_preset_choice(self):
        self.queue(["/Game/S1"])
        master.schedule_jobs()
        self.assertNotIn("output_dir", self.sent[0][1])

    def test_bad_folders_are_refused(self):
        self.assertEqual(self.post("/save-settings", {"output_root": "Renders"}).status_code, 400)
        self.assertEqual(self.post("/launch", {**self.BATCH, "sequences": ["/Game/S"], "output_dir": "x|y"}).status_code, 400)

    def test_edit_window_changes_the_folder(self):
        self.queue(["/Game/S1"], retries=0)
        master.schedule_jobs()
        self.report("A", "FAILED", "q1-1", 1)
        resp = self.post("/retry-job", {"id": 1, "changes": {"output_dir": "K:\\Fixed"}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/get-job?id=1", headers=self.auth).get_json()["output_dir"], "K:\\Fixed")

    def test_out_of_memory_moves_to_another_computer(self):
        self.queue(["/Game/S1"], retries=3)
        master.schedule_jobs()
        node = self.sent[0][0]
        other = "B" if node == "A" else "A"
        self.set_stage(other, "RENDERING")              # the other computer is busy for now
        master.record_results(node, f"boot-{node}", [{"seq": 1, "job_id": "q1-1", "project": "P", "sequence": "/Game/S1",
                                                      "status": "FAILED", "detail": "Ran out of memory", "out_of_memory": True}])
        self.set_stage(node, "IDLE")
        master.schedule_jobs()
        self.assertEqual(len(self.sent), 1)              # not sent back to the computer that ran out of memory
        self.set_stage(other, "IDLE")
        master.schedule_jobs()
        self.assertEqual(self.sent[-1][0], other)

    def test_out_of_memory_with_one_computer_stops_and_says_why(self):
        self.queue(["/Game/S1"], retries=3, nodes=["A"])
        master.schedule_jobs()
        master.record_results("A", "boot-A", [{"seq": 1, "job_id": "q1-1", "project": "P", "sequence": "/Game/S1",
                                               "status": "FAILED", "detail": "Ran out of memory", "out_of_memory": True}])
        job = self.jobs()[1]
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("ran out of memory, so it was not tried again", job["detail"])


if __name__ == "__main__":
    unittest.main()
