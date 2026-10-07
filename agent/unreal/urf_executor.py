"""Unreal Render Farm executor for Movie Render Queue (runs INSIDE Unreal).

Started by the agent with:
    UnrealEditor-Cmd.exe <project> <map> -game
        -MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelinePythonHostExecutor
        -ExecutorPythonClass=/Engine/PythonTypes.URFExecutor
        -LevelSequence=<sequence> -MoviePipelineConfig=<config preset>
        [-URFStart=<first frame> -URFEnd=<last frame>] [-URFWarmup=<frames>] -URFJob=<id>
        [-URFAutoSplit=<pieces> -URFAutoIndex=<n> -URFMinChunk=<frames>]
            (automatic sharing: read the shot's frames, plan the pieces, render piece n)
        [-URFPrepare=1]
            (warm the cache: render one frame of each camera cut to a throw-away folder)

It renders the sequence with the given config preset, limited to the task's frame range, then prints
one URF_RESULT line (and URF_PROGRESS lines while rendering) that the agent reads from stdout.
Modelled on Epic's MoviePipelineExampleRuntimeExecutor.
"""
import os
import shutil
import tempfile
import traceback
import types

import unreal

import time

from urf_mrq_common import (MRQ_END_EXCLUSIVE, PROGRESS_TAG, RESULT_TAG, VIDEO_OUTPUT_CLASSES, bad_files_report,
                            parse_init_time, phase_of,
                            choose_output_dir, drive_kind, is_image_output, preset_cost_notes, cuts_used, mrq_range, plan_auto_split,
                             prepare_frames, progress_line, range_line, result_line, saved_folder, still_bad_files, written_folder,
                             task_from_params)

# Everything the executor remembers during a render. NOT on `self`: Unreal hands Python a fresh wrapper
# object for each call into this class, so plain attributes set on self in one call are gone in the
# next (seen 2026-10-05: AttributeError 'last_percent' / 'prepare_dir'). Only uproperty fields live on
# the Unreal object itself. Unreal runs one executor per process, so module state is safe.
RUN = types.SimpleNamespace()

HEARTBEAT_SECONDS = 30  # a progress line at least this often while the engine ticks (the agent's watchdog)

CUT_TRACK_CLASSES = ("MovieSceneCinematicShotTrack", "MovieSceneCameraCutTrack")


def report(line):
    unreal.log(line)
    # Unreal's stdout reaches the agent through a pipe, which Windows buffers: while rendering Unreal logs
    # so little that progress lines arrived in bursts or only at the end (seen 2026-10-05: the card showed
    # "loading" for the whole render). The latest line also goes to a small file the agent reads every 2 s.
    path = getattr(RUN, "status_file", "")
    target = path if line.startswith(PROGRESS_TAG) else (path + ".result" if line.startswith(RESULT_TAG) else "")
    if path and target:
        try:
            with open(target + ".tmp", "w", encoding="utf-8") as f:
                f.write(line)
            os.replace(target + ".tmp", target)
        except OSError:
            pass


def _tracks_of_type(sequence, cls):
    for name in ("find_tracks_by_type", "find_master_tracks_by_type"):  # UE 5.2+ / older spelling
        finder = getattr(sequence, name, None)
        if finder:
            try:
                return list(finder(cls))
            except Exception:
                continue
    return []


@unreal.uclass()
class URFExecutor(unreal.MoviePipelinePythonHostExecutor):
    active_pipeline = unreal.uproperty(unreal.MoviePipeline)
    # a uproperty, so Unreal keeps it alive. Not called pipeline_queue: the base class has that one, and
    # Unreal refuses to load a class that redeclares it (seen 2026-10-05: 'No class set in Python Host Executor')
    farm_queue = unreal.uproperty(unreal.MoviePipelineQueue)

    def _post_init(self):
        self._reset()

    def _reset(self):
        # Called at the start of execute_delayed (Unreal does not run _post_init for the executor it
        # creates from -ExecutorPythonClass). State lives in RUN, see above.
        self.active_pipeline = None
        self.farm_queue = None
        RUN.task = None
        RUN.status_file = ""
        RUN.output_dir = ""      # folder the farm chose for the frames ("" = the preset's own)
        RUN.output_folder = ""   # where the frames go, as shown on the dashboard
        RUN.drop_video = False   # split piece: leave out the preset's video outputs
        RUN.notes = []           # said in the result (where the frames went, what was left out)
        RUN.result_sent = False  # one result per run, never a second contradicting one
        RUN.finished = False
        RUN.next_prepare = False  # start the next prepare frame on the next engine tick
        RUN.last_percent = -1.0
        RUN.last_report = 0.0
        RUN.progress_error_logged = False
        RUN.prepare_note = ""
        RUN.sequence_path = ""
        RUN.preset = None
        RUN.world = None
        RUN.prepare_left = []    # prepare mode: frames still to render
        RUN.prepare_done = 0
        RUN.prepare_dir = ""

    def _report_result(self, line):
        if getattr(RUN, "result_sent", False):
            return  # the outcome is decided; a later error must not flip it
        RUN.result_sent = True
        report(line)

    def _fail(self, message):
        unreal.log_error(f"URF executor: {message}")
        task = getattr(RUN, "task", None)
        start = task["start"] if task else None
        end = task["end"] if task else None
        self._report_result(result_line(False, {}, start, end, error=message))
        self._finish()

    def _finish(self):
        self.active_pipeline = None
        if getattr(RUN, "finished", False):
            return
        RUN.finished = True
        if getattr(RUN, "prepare_dir", ""):
            shutil.rmtree(RUN.prepare_dir, ignore_errors=True)
        self.on_executor_finished_impl()

    def _shot_range(self, sequence_path, config):
        """Inclusive first/last frame Movie Render Queue would render: the preset's custom range when
        it sets one, else the sequence's playback range (whose end Sequencer keeps exclusive)."""
        output = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
        if output.use_custom_playback_range:
            end = output.custom_end_frame - (1 if MRQ_END_EXCLUSIVE else 0)
            return output.custom_start_frame, end
        sequence = unreal.load_asset(sequence_path)
        if sequence is None:
            raise RuntimeError(f"Level Sequence not found: {sequence_path}")
        try:
            start, end = sequence.get_playback_start(), sequence.get_playback_end()
        except AttributeError:  # older API spelling
            start = unreal.MovieSceneSequenceExtensions.get_playback_start(sequence)
            end = unreal.MovieSceneSequenceExtensions.get_playback_end(sequence)
        return start, end - 1

    def _project_dir(self):
        """The project's folder as a full path (where frames go when the preset's folder is local)"""
        try:
            return unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
        except Exception:
            return ""

    def _find_output_folder(self, preset_folder):
        """Where the frames go, for the dashboard. Only shown to people: never a reason to fail."""
        try:
            level = ""
            try:
                level = RUN.world.get_name() if RUN.world else ""
            except Exception:
                pass
            RUN.output_folder = saved_folder(
                RUN.output_dir or preset_folder, self._project_dir(),
                {"sequence_name": RUN.sequence_path.rsplit("/", 1)[-1].split(".")[0], "level_name": level})
            if RUN.output_folder and not RUN.task["prepare"]:
                unreal.log(f"URF executor: frames are saved to {RUN.output_folder}")
        except Exception:
            RUN.output_folder = ""
            unreal.log_warning("URF executor: could not work out the output folder:\n" + traceback.format_exc())

    def _cut_frames(self, sequence_path):
        """First frame of every camera cut / shot section of the sequence (its own frame numbers).
        Never fatal: no cuts just means splitting on even frame counts."""
        cuts = set()
        try:
            sequence = unreal.load_asset(sequence_path)
            for class_name in CUT_TRACK_CLASSES:
                cls = getattr(unreal, class_name, None)
                if sequence is None or cls is None:
                    continue
                tracks = _tracks_of_type(sequence, cls)
                getter = getattr(sequence, "get_camera_cut_track", None)
                if class_name == "MovieSceneCameraCutTrack" and getter:
                    try:
                        track = getter()
                        if track and track not in tracks:
                            tracks.append(track)
                    except Exception:
                        pass
                for track in tracks:
                    for section in track.get_sections():
                        try:
                            if section.is_active() and section.has_start_frame():
                                cuts.add(int(section.get_start_frame()))
                        except Exception:
                            continue
        except Exception:
            unreal.log_warning("URF executor: could not read camera cuts:\n" + traceback.format_exc())
        return sorted(cuts)

    def _new_job(self, start=None, end=None, output_dir=""):
        """One Movie Render Queue job for the sequence with the preset, limited to start-end"""
        job = self.farm_queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
        job.sequence = unreal.SoftObjectPath(RUN.sequence_path)
        if RUN.world:
            job.map = unreal.SoftObjectPath(RUN.world.get_path_name())
        job.set_configuration(RUN.preset)
        config = job.get_configuration()
        output = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
        if start is not None:
            custom_start, custom_end = mrq_range(start, end)
            output.use_custom_playback_range = True
            output.custom_start_frame = custom_start
            output.custom_end_frame = custom_end
            unreal.log(f"URF executor: frames {start}-{end} (MRQ custom range {custom_start}..{custom_end})")
        output_dir = output_dir or RUN.output_dir
        if output_dir:
            output.output_directory = unreal.DirectoryPath(output_dir)
        if RUN.drop_video:
            for setting in list(config.get_all_settings()):
                if setting.get_class().get_name() in VIDEO_OUTPUT_CLASSES:
                    config.remove_setting(setting)
        if RUN.task["warmup"]:
            # Settle temporal effects (TAA, Lumen, motion blur history) before the first written frame
            aa = config.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
            aa.engine_warm_up_count = max(aa.engine_warm_up_count, RUN.task["warmup"])
            aa.render_warm_up_count = max(aa.render_warm_up_count, RUN.task["warmup"])
        config.initialize_transient_settings()
        return job

    def _start(self, job):
        RUN.last_percent = -1.0
        self.active_pipeline = unreal.new_object(
            self.target_pipeline_class, outer=RUN.world, base_type=unreal.MoviePipeline)
        self.active_pipeline.on_movie_pipeline_work_finished_delegate.add_function_unique(
            self, "on_movie_pipeline_finished")
        when = parse_init_time(os.environ.get("URF_INIT_TIME"))
        if when:
            try:
                # every computer of a split shot stamps {date}/{time} in file names with the same time
                self.active_pipeline.set_initialization_time(unreal.DateTime(*when))
            except Exception:
                unreal.log_warning("URF executor: could not set the shared start time:\n" + traceback.format_exc())
        self.active_pipeline.initialize(job)

    def _start_next_prepare(self):
        frame = RUN.prepare_left.pop(0)
        unreal.log(f"URF executor: preparing frame {frame} "
                   f"({RUN.prepare_done + 1} of {RUN.prepare_done + len(RUN.prepare_left) + 1})")
        self._start(self._new_job(frame, frame, RUN.prepare_dir))

    @unreal.ufunction(override=True)
    def execute_delayed(self, in_pipeline_queue):
        self._reset()
        try:
            _tokens, _switches, params = unreal.SystemLibrary.parse_command_line(
                unreal.SystemLibrary.get_command_line())
            RUN.task = task_from_params(params)
            RUN.status_file = str(params.get("URFStatusFile", "")).strip('"')
            RUN.sequence_path = params.get("LevelSequence", "").strip('"')
            config_path = params.get("MoviePipelineConfig", "").strip('"')
            if not RUN.sequence_path or not config_path:
                return self._fail("-LevelSequence and -MoviePipelineConfig are required")

            self.farm_queue = unreal.new_object(unreal.MoviePipelineQueue, outer=self)
            RUN.world = self.get_last_loaded_world()

            preset = unreal.load_asset(config_path)
            if preset is None:
                return self._fail(f"Movie Pipeline config preset not found: {config_path}")
            if isinstance(preset, unreal.MoviePipelineQueue):
                # A saved Queue was given instead of a preset: use its first job's settings
                queue_jobs = preset.get_jobs()
                if not queue_jobs:
                    return self._fail(f"The saved Queue {config_path} has no jobs; choose a render preset instead")
                unreal.log(f"URF executor: using the settings of the first job in saved Queue {config_path}")
                preset = queue_jobs[0].get_configuration()
            kind = preset.get_class().get_name()
            if kind not in ("MoviePipelinePrimaryConfig", "MoviePipelineMasterConfig"):
                return self._fail(f"{config_path} is a {kind}, not a Movie Pipeline render preset")
            RUN.preset = preset
            probe = self._new_job()  # the preset as Movie Render Queue sees it, to read its settings
            config = probe.get_configuration()
            names = [s.get_class().get_name() for s in config.get_all_settings()]
            try:
                cost = preset_cost_notes([(s.get_class().get_name(), s) for s in config.get_all_settings()])
                if cost:
                    RUN.notes.append("Slow preset settings: " + "; ".join(cost) + ".")
            except Exception:
                pass  # advice only, never a reason to fail
            video = next((n for n in names if n in VIDEO_OUTPUT_CLASSES), None)
            images = any(is_image_output(n) for n in names)
            preset_output = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting).output_directory
            self.farm_queue.delete_job(probe)
            RUN.output_dir, note = choose_output_dir(os.environ.get("URF_OUTPUT_DIR", ""),
                                                     getattr(preset_output, "path", preset_output),
                                                     self._project_dir(), drive_kind)
            if note:
                RUN.notes.append(note)
                unreal.log(f"URF executor: {note}")
            self._find_output_folder(getattr(preset_output, "path", preset_output))

            if RUN.task["prepare"]:
                # Warm the cache: one frame per camera cut into a throw-away folder. This builds the
                # shaders and textures the real render needs (into the shared cache when one is set).
                first, last = self._shot_range(RUN.sequence_path, config)
                RUN.prepare_left, found = prepare_frames(first, last, self._cut_frames(RUN.sequence_path))
                if found > len(RUN.prepare_left):
                    RUN.prepare_note = (f" Only the first {len(RUN.prepare_left)} of {found} camera cuts were "
                                         "prepared; the rest build their shaders during the render.")
                    unreal.log_warning("URF executor:" + RUN.prepare_note)
                RUN.prepare_dir = os.path.join(tempfile.gettempdir(), "URF_prepare", RUN.task["job_id"] or "job")
                report(range_line(RUN.task["job_id"], first, last, [(f, f) for f in RUN.prepare_left],
                                  "prepare"))
                return self._start_next_prepare()

            if RUN.task["auto_pieces"]:
                # Automatic sharing: every computer of the shot starts at once with its piece number.
                # Each reads the same frames and makes the same plan, then renders its own piece.
                first, last = self._shot_range(RUN.sequence_path, config)
                index = RUN.task["auto_index"]
                cuts = 0
                if video and not images:
                    ranges, note = [(first, last)], f"{video} writes video, so the shot renders whole"
                else:
                    cut_frames = self._cut_frames(RUN.sequence_path)
                    ranges = plan_auto_split(first, last, RUN.task["auto_pieces"], RUN.task["min_chunk"],
                                             cut_frames)
                    cuts = cuts_used(ranges, cut_frames)
                    note = f"{cuts} split(s) on camera cuts" if cuts else ""
                report(range_line(RUN.task["job_id"], first, last, ranges, note, index, cuts))
                if index > len(ranges):
                    message = f"Nothing to render: the shot ({first}-{last}) fits in {len(ranges)} piece(s)"
                    self._report_result(result_line(True, {}, note=message))
                    self._finish()
                    return
                if len(ranges) > 1:
                    RUN.task["start"], RUN.task["end"] = ranges[index - 1]

            if RUN.task["start"] is not None and video:
                if not images:
                    return self._fail(f"{video} writes one video per task; "
                                      "render an image sequence when splitting shots into frame ranges")
                # A piece can't add its frames to one shared video: keep the pictures, skip the video
                RUN.drop_video = True
                RUN.notes.append(f"{video.replace('MoviePipeline', '')} skipped for this piece: a shot split "
                                 "between computers can't share one video file. Make the video from the frames.")
            self._start(self._new_job(RUN.task["start"], RUN.task["end"]))
        except Exception:
            self._fail(traceback.format_exc())

    @unreal.ufunction(override=True)
    def on_begin_frame(self):
        super(URFExecutor, self).on_begin_frame()
        if getattr(RUN, "next_prepare", False):
            RUN.next_prepare = False
            try:
                self._start_next_prepare()
            except Exception:
                self._fail("starting the next prepare frame: " + traceback.format_exc())
            return
        if not self.active_pipeline:
            return
        try:
            self._report_progress()
        except Exception:
            if not getattr(RUN, "progress_error_logged", False):
                RUN.progress_error_logged = True
                unreal.log_warning("URF executor: progress report failed:\n" + traceback.format_exc())

    def _report_progress(self):
        now = time.time()
        percent = max(RUN.last_percent, 0.0)
        current = total = None
        try:
            percent = unreal.MoviePipelineLibrary.get_completion_percentage(self.active_pipeline) * 100
            if RUN.prepare_dir:  # prepare mode: progress over all the frames to prepare
                count = RUN.prepare_done + len(RUN.prepare_left) + 1
                percent = (RUN.prepare_done + percent / 100) / count * 100
            try:
                current, total = unreal.MoviePipelineLibrary.get_overall_output_frames(self.active_pipeline)
            except Exception:
                pass
            extra = self._render_details()
            if RUN.output_folder and not RUN.prepare_dir:
                extra["output"] = RUN.output_folder
            if current and extra.get("phase") == "warmup":
                extra = {k: v for k, v in extra.items() if k not in ("warmup", "warmups")}
                extra["phase"] = "render"
        except Exception:
            extra = {}
            if not RUN.progress_error_logged:  # say why once, keep the heartbeat going
                RUN.progress_error_logged = True
                unreal.log_warning("URF executor: could not read render progress:\n" + traceback.format_exc())
        # A line when the bar moves, and a heartbeat while the engine ticks: one slow frame never looks frozen
        if percent - RUN.last_percent >= 0.5 or now - RUN.last_report >= HEARTBEAT_SECONDS:
            RUN.last_percent = max(RUN.last_percent, percent)
            RUN.last_report = now
            report(progress_line(RUN.last_percent, current, total, **extra))

    def _render_details(self):
        """Phase, Unreal's own time-left estimate and the sample count of the current frame. Never fatal:
        each value is optional (UE 5.6 MoviePipelineLibrary)."""
        library, pipeline, extra = unreal.MoviePipelineLibrary, self.active_pipeline, {}
        warming = False
        try:
            work = library.get_current_segment_work_metrics(pipeline)
            warmups = int(getattr(work, "total_engine_warm_up_frame_count", 0) or 0)
            warmup = int(getattr(work, "engine_warm_up_frame_index", 0) or 0)
            if warmups and warmup < warmups:
                warming = True
                extra.update(warmup=warmup + 1, warmups=warmups)
            samples = int(getattr(work, "total_sub_sample_count", 0) or 0)
            if samples > 1:
                extra.update(sample=int(getattr(work, "output_sub_sample_index", 0) or 0) + 1, samples=samples)
        except Exception:
            pass
        try:
            extra["phase"] = phase_of(library.get_pipeline_state(pipeline), warming)
        except Exception:
            pass
        try:
            remaining = library.get_estimated_time_remaining(pipeline)
            if remaining is not None:
                extra["eta_seconds"] = round(unreal.MathLibrary.get_total_seconds(remaining))
        except Exception:
            pass
        return extra

    @unreal.ufunction(ret=None, params=[unreal.MoviePipelineOutputData])
    def on_movie_pipeline_finished(self, results):
        try:
            self._pipeline_finished(results)
        except Exception:
            # Never leave Unreal open after a render: report what went wrong and finish
            self._fail("after the render: " + traceback.format_exc())

    def _pipeline_finished(self, results):
        files_per_pass = {}
        written = []
        try:
            for shot in results.shot_data:
                for pass_id, pass_data in shot.render_pass_data.items():
                    name = getattr(pass_id, "name", str(pass_id))
                    paths = list(pass_data.file_paths)
                    files_per_pass[name] = files_per_pass.get(name, 0) + len(paths)
                    written.extend(paths)
        except Exception:
            unreal.log_warning("URF executor: could not count output files:\n" + traceback.format_exc())

        if RUN.prepare_dir:
            if not results.success:
                return self._fail("Movie Render Queue reported failure while preparing")
            RUN.prepare_done += 1
            if RUN.prepare_left:
                # the finished pipeline is still tearing down: start the next one on the next engine tick
                self.active_pipeline = None
                RUN.next_prepare = True
                return None
            self._report_result(result_line(True, {}, note=f"Prepared {RUN.prepare_done} camera cut(s): "
                                                          "shaders and textures are now cached." + RUN.prepare_note))
            return self._finish()

        # Check every written frame is really on the drive (a NAS hiccup can leave missing or 0-byte files)
        bad = bad_files_report(still_bad_files(written, os.path.getsize), os.path.getsize)
        if bad["count"]:
            unreal.log_error(f"URF executor: {bad['count']} output file(s) missing or empty: {bad['examples']}")
        failure = "" if results.success else "Movie Render Queue reported failure"
        self._report_result(result_line(results.success, files_per_pass, RUN.task["start"], RUN.task["end"],
                                        failure, note=" ".join(RUN.notes), bad_files=bad,
                                        output_folder=written_folder(written) or RUN.output_folder))
        self._finish()

    @unreal.ufunction(override=True)
    def is_rendering(self):
        return self.active_pipeline is not None or getattr(RUN, "next_prepare", False)
