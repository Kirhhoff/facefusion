#!/usr/bin/env python3
"""
Video Face-Swap Pipeline Script

Workaround for FaceFusion not detecting faces in certain video encodings:
1. Extract video frames as images
2. Split frames into time-segment batches
3. Run multiple FaceFusion job-run processes in parallel
4. Reassemble processed frames into video with original audio

Usage:
    python video_face_swap.py \\
        --video input.mp4 \\
        --source face.jpg \\
        --output result.mp4 \\
        --workers 3 \\
        --start-time 00:00:10 \\
        --end-time 00:00:30 \\
        --config-path facefusion.ini

    # Multiple source images (averaged to create a composite reference face)
    python video_face_swap.py \\
        --video input.mp4 \\
        --source face1.jpg face2.jpg face3.jpg \\
        --output result.mp4 \\
        --workers 4
"""

import argparse
import json
import os
import subprocess
import sys
import time
from configparser import ConfigParser
from datetime import datetime
from glob import glob


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd: list, *, check: bool = True, capture: bool = False, **kwargs):
    """Run a command and optionally return stdout."""
    if capture:
        result = subprocess.run(cmd, check=check, capture_output=True, text=True, **kwargs)
        return result.stdout.strip()
    else:
        subprocess.run(cmd, check=check, **kwargs)


def get_video_fps(video_path: str) -> float:
    """Use ffprobe to detect the video frame rate."""
    out = run_cmd([
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=r_frame_rate',
        '-of', 'json',
        video_path
    ], capture=True)
    info = json.loads(out)
    rate_str = info['streams'][0]['r_frame_rate']  # e.g. "30000/1001"
    num, den = rate_str.split('/')
    return float(num) / float(den)


def get_video_duration(video_path: str) -> float:
    """Use ffprobe to get video duration in seconds."""
    out = run_cmd([
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'json',
        video_path
    ], capture=True)
    info = json.loads(out)
    return float(info['format']['duration'])


def time_to_seconds(time_str: str) -> float:
    """Convert HH:MM:SS or SS format to seconds."""
    if time_str is None:
        return None
    parts = time_str.split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    else:
        return float(time_str[0])


def seconds_to_timecode(seconds: float) -> str:
    """Convert seconds to HH:MM:SS.mmm format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'


def _read_config_step_args(config_path: str) -> dict:
    """
    Read **all** step-level args from facefusion.ini so that they are present in
    the job JSON.  Without this, FaceFusion's ``apply_args(step_args,
    state_manager.set_item)`` would call ``args.get('face_swapper_model')`` →
    ``None`` and **overwrite** the default value that was set by ``init_item``
    during CLI parsing.

    Instead of hard-coding individual keys, this function reads every non-empty
    key-value pair from the step-relevant INI sections and performs automatic
    type inference so that the values match what ``argparse`` would normally
    produce.  This way, any future parameter added to ``facefusion.ini`` will
    be picked up automatically — no code change required.

    Parameters
    ----------
    config_path : str
        Path to ``facefusion.ini``.

    Returns
    -------
    dict
        Step args dict with config-derived values.
    """
    cp = ConfigParser()
    cp.read(config_path, encoding='utf-8')

    # Sections that contain step-level configuration (i.e. keys registered via
    # ``register_step_keys`` in FaceFusion).  Sections like [execution],
    # [memory], [uis], [download], [benchmark], [misc] are job-level and will
    # be handled by the worker's own ``--config-path`` flag, so we skip them.
    STEP_SECTIONS = [
        'face_detector',
        'face_landmarker',
        'face_selector',
        'face_masker',
        'voice_extractor',
        'frame_extraction',
        'output_creation',
        'processors',
    ]

    # Keys that we set manually when building step args — they should NOT be
    # overwritten by config values.
    MANUAL_KEYS = {
        'source_paths',
        'target_path',
        'output_path',
        'processors',
    }

    # Suffixes that indicate a float value (matching FaceFusion's
    # ``register_args`` conventions where ``type = float`` is used).
    FLOAT_SUFFIXES = (
        '_score',
        '_distance',
        '_weight',
        '_factor',
        '_blend',
        '_blur',
        '_scale',
        '_fps',
        '_direction',
        '_volume',
        '_morph',
    )

    # Suffixes that indicate an int value (``type = int`` in register_args).
    INT_SUFFIXES = (
        '_margin',
        '_angles',
        '_start',
        '_end',
        '_quality',
        '_count',
        '_position',
        '_frame_number',
        '_thread_count',
        '_limit',
        '_age_start',
        '_age_end',
        '_padding',
        '_device_ids',
    )

    # Known list-type keys where ``nargs = '+'`` and items are strings.
    STR_LIST_KEYS = {
        'face_mask_types',
        'face_mask_areas',
        'face_mask_regions',
        'face_selector_order',
        'face_selector_gender',
        'face_selector_race',
        'face_debugger_items',
        'expression_restorer_areas',
    }

    # Known list-type keys where ``nargs = '+'`` and items are ints.
    INT_LIST_KEYS = {
        'face_detector_angles',
        'face_detector_margin',
        'face_mask_padding',
        'execution_device_ids',
    }

    step_args: dict = {}

    for section in STEP_SECTIONS:
        if not cp.has_section(section):
            continue
        for key in cp.options(section):
            if key in MANUAL_KEYS:
                continue
            raw = cp.get(section, key).strip()
            if not raw:
                # Empty value in INI → skip (FaceFusion will use its built-in default)
                continue

            # ---- Type inference ----
            if key in INT_LIST_KEYS:
                step_args[key] = [int(x) for x in raw.split()]
            elif key in STR_LIST_KEYS:
                step_args[key] = raw.split()
            elif key in INT_SUFFIXES and any(key.endswith(s) for s in INT_SUFFIXES):
                try:
                    step_args[key] = int(raw)
                except ValueError:
                    step_args[key] = raw
            elif any(key.endswith(s) for s in FLOAT_SUFFIXES):
                try:
                    step_args[key] = float(raw)
                except ValueError:
                    step_args[key] = raw
            else:
                # Default: keep as string (model names, modes, etc.)
                step_args[key] = raw

    return step_args


# ---------------------------------------------------------------------------
# Step 1: Extract frames & audio
# ---------------------------------------------------------------------------

def extract_frames(video_path: str, work_dir: str, start_time: str = None, end_time: str = None, turbo_mode: bool = False) -> tuple:
    """
    Extract video frames as PNG images and audio track.
    Returns (frames_dir, audio_path_or_None, fps).
    """
    frames_dir = os.path.join(work_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    fps = get_video_fps(video_path)
    print(f'[Info] Detected video FPS: {fps:.3f}')

    # --- Extract frames ---
    cmd = ['ffmpeg', '-y']
    if start_time:
        cmd += ['-ss', start_time]
    if end_time:
        cmd += ['-to', end_time]
    cmd += [
        '-i', video_path,
        '-vsync', '0',
        '-qmin', '1', '-q:v', '1',  # high quality PNG
        os.path.join(frames_dir, 'frame_%08d.png')
    ]
    print(f'[Step 1] Extracting frames ...')
    run_cmd(cmd)

    frame_count = len(glob(os.path.join(frames_dir, 'frame_*.png')))
    print(f'[Step 1] Extracted {frame_count} frames')

    # --- Extract audio ---
    audio_path = os.path.join(work_dir, 'audio.aac')
    cmd_audio = ['ffmpeg', '-y']
    if start_time:
        cmd_audio += ['-ss', start_time]
    if end_time:
        cmd_audio += ['-to', end_time]
    cmd_audio += [
        '-i', video_path,
        '-vn', '-acodec', 'copy',
        audio_path
    ]
    try:
        run_cmd(cmd_audio, check=True)
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            audio_path = None
            print('[Step 1] No audio track found or audio is empty')
        else:
            print(f'[Step 1] Audio extracted to {audio_path}')
    except subprocess.CalledProcessError:
        audio_path = None
        print('[Step 1] No audio track found, skipping')

    return frames_dir, audio_path, fps


# ---------------------------------------------------------------------------
# Step 1.5: Detect keyframes and create symlink directories
# ---------------------------------------------------------------------------

def detect_keyframe_indices(video_path: str, start_time: str = None, end_time: str = None) -> set:
    """
    Use ffprobe to detect which frame numbers are keyframes (I-frames).
    Returns a set of 1-based frame indices matching the extracted frame_%08d.png naming.
    """
    cmd = ['ffprobe', '-v', 'error']
    
    # Add time range options for better compatibility
    if start_time:
        cmd.extend(['-ss', start_time])
    if end_time:
        cmd.extend(['-to', end_time])
    
    # Add the rest of the ffprobe options
    cmd.extend([
        '-select_streams', 'v:0',
        '-show_frames',
        '-show_entries', 'frame=pict_type',
        '-of', 'csv=p=0',
        video_path
    ])

    try:
        out = run_cmd(cmd, capture=True)
        if not out:
            return set()

        keyframe_indices = set()
        for i, line in enumerate(out.splitlines(), start=1):
            pict_type = line.strip()
            if pict_type == 'I':
                keyframe_indices.add(i)

        return keyframe_indices
    except subprocess.CalledProcessError as e:
        print(f"ffprobe command failed: {' '.join(cmd)}")
        print(f"Error output: {e.stderr}")
        return set()


def _build_ffprobe_interval(start_time: str = None, end_time: str = None) -> str:
    """Build ffprobe -read_intervals value like '%+00:01:00' or '%00:00:10%00:00:30'."""
    # Handle None values properly
    start = start_time if start_time is not None else ''
    end = end_time if end_time is not None else ''

    # When start is empty or the beginning of the video, use %+duration format
    if not start or start == '00:00:00':
        if end:
            return f'%+{end}'
        return '%+99999'  # effectively read all

    if end:
        return f'%{start}%{end}'

    return f'%{start}%+99999'


def collect_keyframes(frames_dir: str, work_dir: str, keyframe_indices: set):
    """
    Create keyframes_original/ with symlinks to original keyframes.
    Returns (keyframes_original_dir, list_of_keyframe_names).
    """
    keyframes_orig_dir = os.path.join(work_dir, 'keyframes_original')
    os.makedirs(keyframes_orig_dir, exist_ok=True)

    all_frames = sorted(glob(os.path.join(frames_dir, 'frame_*.png')))
    keyframe_names = []

    for i, frame_path in enumerate(all_frames, start=1):
        if i in keyframe_indices:
            name = os.path.basename(frame_path)
            link_path = os.path.join(keyframes_orig_dir, name)
            if not os.path.exists(link_path):
                os.symlink(os.path.abspath(frame_path), link_path)
            keyframe_names.append(name)

    print(f'[Step 1.5] Detected {len(keyframe_names)} keyframes out of {len(all_frames)} total frames '
          f'({100 * len(keyframe_names) / max(len(all_frames), 1):.1f}%)')
    print(f'  Original keyframes → {keyframes_orig_dir}')

    return keyframes_orig_dir, keyframe_names


def collect_swapped_keyframes(segments: list, work_dir: str, keyframe_names: list):
    """
    After face-swapping, symlink the swapped versions of keyframes into keyframes_swapped/.
    Zero extra computation — just picks from existing output.
    """
    keyframes_swap_dir = os.path.join(work_dir, 'keyframes_swapped')
    os.makedirs(keyframes_swap_dir, exist_ok=True)

    keyframe_set = set(keyframe_names)
    found = 0

    for seg in segments:
        for name in seg['frame_names']:
            if name in keyframe_set:
                output_frame = os.path.join(seg['output_dir'], name)
                link_path = os.path.join(keyframes_swap_dir, name)
                if os.path.exists(output_frame) and not os.path.exists(link_path):
                    os.symlink(os.path.abspath(output_frame), link_path)
                    found += 1

    print(f'[Step 4.5] Collected {found}/{len(keyframe_names)} swapped keyframes → {keyframes_swap_dir}')


# ---------------------------------------------------------------------------
# Step 2: Split frames into contiguous time-segments
# ---------------------------------------------------------------------------

def split_frames_into_segments(frames_dir: str, work_dir: str, n_workers: int) -> list:
    """
    Split frames into N contiguous segments.
    Creates batch_N/ directories with symlinks.
    Returns list of dicts: {batch_dir, output_dir, frame_count, frame_names}
    """
    all_frames = sorted(glob(os.path.join(frames_dir, 'frame_*.png')))
    total = len(all_frames)

    if total == 0:
        print('[Error] No frames found!')
        sys.exit(1)

    # Clamp workers
    n_workers = min(n_workers, total)

    # Divide into contiguous chunks
    chunk_size = total // n_workers
    remainder = total % n_workers

    segments = []
    offset = 0
    for i in range(n_workers):
        # Distribute remainder frames to first workers
        size = chunk_size + (1 if i < remainder else 0)
        chunk_frames = all_frames[offset:offset + size]
        offset += size

        batch_dir = os.path.join(work_dir, f'batch_{i}')
        output_dir = os.path.join(work_dir, f'output_{i}')
        os.makedirs(batch_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # Create symlinks in batch dir
        frame_names = []
        for frame_path in chunk_frames:
            name = os.path.basename(frame_path)
            link_path = os.path.join(batch_dir, name)
            if not os.path.exists(link_path):
                os.symlink(os.path.abspath(frame_path), link_path)
            frame_names.append(name)

        segments.append({
            'batch_dir': batch_dir,
            'output_dir': output_dir,
            'frame_count': size,
            'frame_names': frame_names,
            'worker_id': i,
        })

    print(f'[Step 2] Split {total} frames into {n_workers} segments:')
    for seg in segments:
        first = seg['frame_names'][0] if seg['frame_names'] else '?'
        last = seg['frame_names'][-1] if seg['frame_names'] else '?'
        # Verify symlinks actually exist
        batch_files = sorted(glob(os.path.join(seg['batch_dir'], 'frame_*.png')))
        print(f'  Worker {seg["worker_id"]}: {seg["frame_count"]} frames ({first} ~ {last}), '
              f'batch_dir has {len(batch_files)} files')

    return segments


# ---------------------------------------------------------------------------
# Step 3 & 4: Launch workers with progress monitoring
# ---------------------------------------------------------------------------

def _write_facefusion_job_json(jobs_path: str, job_id: str, steps: list):
    """
    Write a FaceFusion job JSON file directly into the queued directory.

    This bypasses the slow ``job-create`` -> ``job-add-step`` x N -> ``job-submit``
    CLI pipeline.  Each step carries the full ``source_paths`` list so that
    ``extract_source_face`` can average multiple reference faces.

    The step format is identical to what ``headless-run`` produces internally
    (see ``core.process_headless``): each step has ``source_paths`` (list of
    source image paths), ``target_path`` (single target), and ``output_path``.
    The difference is that we batch many steps into one job and run them with
    a single ``job-run`` invocation, so the model is loaded only once.

    Parameters
    ----------
    jobs_path : str
        Root of the FaceFusion jobs directory (passed via ``--jobs-path``).
    job_id : str
        Unique job identifier (used as the JSON filename stem).
    steps : list[dict]
        Each element is ``{'args': {...}, 'status': 'queued'}``.
    """
    queued_dir = os.path.join(jobs_path, 'queued')
    os.makedirs(queued_dir, exist_ok=True)

    job_data = {
        'version': '1',
        'date_created': datetime.now().isoformat(),
        'date_updated': None,
        'steps': steps,
    }
    job_json_path = os.path.join(queued_dir, f'{job_id}.json')
    with open(job_json_path, 'w') as jf:
        json.dump(job_data, jf, indent=2)


def launch_workers(segments: list, source_paths: list, config_path: str, facefusion_script: str, work_dir: str, batch_size: int = 300, face_swap_debug: bool = False) -> list:
    """
    Launch FaceFusion worker subprocesses.

    Multiple source images are supported — they are passed as ``source_paths``
    to every step so that ``extract_source_face`` computes an average face
    embedding across all source images (same behaviour as ``headless-run -s
    img1 -s img2 ...``).

    Each worker's frames are further split into mini-batches (default 300
    frames).  For each mini-batch a FaceFusion job JSON is written *directly*
    into the queued directory (bypassing the slow per-step CLI calls).  A
    single ``job-run`` invocation then processes all frames — the model loads
    only once per mini-batch.
    """
    processes = []
    source_abs_list = [os.path.abspath(p) for p in source_paths]
    config_abs = os.path.abspath(config_path)
    facefusion_dir = os.path.dirname(os.path.abspath(facefusion_script))
    python_exe = sys.executable

    # Read step-level config defaults from facefusion.ini so that critical
    # keys like ``face_swapper_model`` are present in every step dict.
    # Without this, FaceFusion's ``apply_args(step_args, state_manager.set_item)``
    # would overwrite the init-time default with ``None``.
    config_step_defaults = _read_config_step_args(config_path)

    for seg in segments:
        output_dir_abs = os.path.abspath(seg['output_dir'])
        batch_dir_abs = os.path.abspath(seg['batch_dir'])

        # Split this worker's frames into mini-batches
        frame_names = seg['frame_names']
        mini_batches = [frame_names[i:i + batch_size] for i in range(0, len(frame_names), batch_size)]

        # Build job JSON files & worker shell script
        script_path = os.path.join(work_dir, f'run_worker_{seg["worker_id"]}.sh')
        with open(script_path, 'w') as f:
            f.write('#!/bin/bash\n')
            f.write(f'cd "{facefusion_dir}"\n\n')
            for mb_idx, mb_frames in enumerate(mini_batches):
                jobs_path = os.path.join(work_dir, f'jobs_{seg["worker_id"]}_{mb_idx}')
                mb_job_id = f'worker-{seg["worker_id"]}-mb-{mb_idx}'

                # Build steps — each frame is one step, all steps share the same source_paths
                steps = []
                for frame_name in mb_frames:
                    frame_path = os.path.join(batch_dir_abs, frame_name)
                    output_frame = os.path.join(output_dir_abs, frame_name)
                    step_args = {
                        'source_paths': source_abs_list,
                        'target_path': frame_path,
                        'output_path': output_frame,
                        'processors': ['face_swapper'],
                    }
                    # Merge config defaults (e.g. face_swapper_model) so that
                    # apply_args does not overwrite them with None.
                    step_args.update(config_step_defaults)
                    step = {
                        'args': step_args,
                        'status': 'queued',
                    }
                    if face_swap_debug:
                        step['args']['face_swap_debug'] = True
                    steps.append(step)

                # Write the job JSON directly into the queued directory
                _write_facefusion_job_json(jobs_path, mb_job_id, steps)

                # Shell command: just run the job
                f.write(
                    f'echo "[Worker {seg["worker_id"]}] mini-batch {mb_idx + 1}/{len(mini_batches)}'
                    f' ({len(mb_frames)} frames)"\n'
                )
                f.write(
                    f'"{python_exe}" "{facefusion_script}" job-run'
                    f' {mb_job_id}'
                    f' --config-path "{config_abs}"'
                    f' --jobs-path "{jobs_path}"'
                    '\n\n'
                )
        os.chmod(script_path, 0o755)

        log_path = os.path.join(work_dir, f'worker_{seg["worker_id"]}.log')
        log_file = open(log_path, 'w')

        # Debug info
        print(f'[Step 3] Launching worker {seg["worker_id"]}: {seg["frame_count"]} frames '
              f'in {len(mini_batches)} mini-batches of ~{batch_size}')
        print(f'  Script:        {script_path}')
        print(f'  Source:        {", ".join(source_abs_list)} (exists: {all(os.path.isfile(s) for s in source_abs_list)})')
        print(f'  Config:        {config_abs} (exists: {os.path.isfile(config_abs)})')
        print(f'  Output dir:    {output_dir_abs}')
        print(f'  Log:           {log_path}')

        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'

        proc = subprocess.Popen(
            ['bash', script_path],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=facefusion_dir,
            env=env,
        )
        proc._log_file = log_file
        proc._worker_id = seg['worker_id']
        processes.append(proc)

    return processes


def monitor_progress(segments: list, processes: list):
    """
    Monitor progress of each worker by counting output files.
    Each worker gets its own tqdm progress bar.
    """
    try:
        from tqdm import tqdm
        has_tqdm = True
    except ImportError:
        has_tqdm = False
        print('[Warning] tqdm not installed, falling back to simple progress output')

    total_frames = sum(seg['frame_count'] for seg in segments)

    if has_tqdm:
        # Create per-worker bars + overall bar
        bars = []
        for seg in segments:
            bar = tqdm(
                total=seg['frame_count'],
                desc=f'Worker {seg["worker_id"]}',
                position=seg['worker_id'],
                leave=True,
                ncols=100,
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]'
            )
            bars.append(bar)

        overall_bar = tqdm(
            total=total_frames,
            desc='Overall ',
            position=len(segments),
            leave=True,
            ncols=100,
            colour='green',
            bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]'
        )

        prev_counts = [0] * len(segments)

        while True:
            all_done = all(p.poll() is not None for p in processes)
            overall_count = 0

            for i, seg in enumerate(segments):
                count = len(glob(os.path.join(seg['output_dir'], 'frame_*.png')))
                delta = count - prev_counts[i]
                if delta > 0:
                    bars[i].update(delta)
                    prev_counts[i] = count
                overall_count += count

            overall_delta = overall_count - overall_bar.n
            if overall_delta > 0:
                overall_bar.update(overall_delta)

            if all_done:
                # Final count update
                for i, seg in enumerate(segments):
                    count = len(glob(os.path.join(seg['output_dir'], 'frame_*.png')))
                    delta = count - prev_counts[i]
                    if delta > 0:
                        bars[i].update(delta)
                overall_final = sum(len(glob(os.path.join(seg['output_dir'], 'frame_*.png'))) for seg in segments)
                overall_delta = overall_final - overall_bar.n
                if overall_delta > 0:
                    overall_bar.update(overall_delta)
                break

            time.sleep(1.0)

        for bar in bars:
            bar.close()
        overall_bar.close()
        # Move cursor below all bars
        print('\n' * (len(segments) + 1))

    else:
        # Simple fallback: periodic text output
        while True:
            all_done = all(p.poll() is not None for p in processes)
            status_parts = []
            overall = 0
            for seg in segments:
                count = len(glob(os.path.join(seg['output_dir'], 'frame_*.png')))
                status_parts.append(f'W{seg["worker_id"]}:{count}/{seg["frame_count"]}')
                overall += count
            print(f'\r[Progress] {" | ".join(status_parts)} | Total: {overall}/{total_frames}', end='', flush=True)

            if all_done:
                print()
                break
            time.sleep(2.0)


# ---------------------------------------------------------------------------
# Step 5: Reassemble video
# ---------------------------------------------------------------------------

def reassemble_video(segments: list, output_path: str, fps: float, audio_path: str = None, work_dir: str = None):
    """Collect all processed frames in order and encode back to video."""
    print('[Step 5] Reassembling video ...')

    # Collect all output frames in order
    ordered_dir = os.path.join(work_dir, 'ordered_output')
    os.makedirs(ordered_dir, exist_ok=True)

    frame_idx = 1
    missing_count = 0
    total_count = 0

    for seg in segments:
        for name in seg['frame_names']:
            output_frame = os.path.join(seg['output_dir'], name)
            # If FaceFusion output exists, use it; otherwise fall back to original frame
            if os.path.exists(output_frame):
                src = output_frame
            else:
                # Fallback: use original frame from batch_dir (symlink -> frames/)
                original = os.path.join(seg['batch_dir'], name)
                if os.path.exists(original):
                    src = original
                    missing_count += 1
                else:
                    print(f'[Warning] Frame missing: {name}')
                    continue

            # Create sequentially numbered symlink for ffmpeg
            link_name = f'frame_{frame_idx:08d}.png'
            link_path = os.path.join(ordered_dir, link_name)
            if not os.path.exists(link_path):
                os.symlink(os.path.abspath(src), link_path)
            frame_idx += 1
            total_count += 1

    if missing_count > 0:
        print(f'[Warning] {missing_count} frames were not processed (using originals as fallback)')

    print(f'[Step 5] Encoding {total_count} frames at {fps:.3f} fps ...')

    # Build ffmpeg command
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    cmd = [
        'ffmpeg', '-y',
        '-framerate', str(fps),
        '-i', os.path.join(ordered_dir, 'frame_%08d.png'),
    ]

    if audio_path and os.path.exists(audio_path):
        cmd += ['-i', audio_path, '-c:a', 'aac', '-shortest']

    cmd += [
        '-c:v', 'mpeg4',
        '-qscale:v', '2',
        '-pix_fmt', 'yuv420p',
        output_path
    ]

    run_cmd(cmd)
    print(f'[Step 5] Output video saved to: {output_path}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Video Face-Swap Pipeline: extract frames → parallel FaceFusion → reassemble',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage — work dir auto-named as ./input_face/
  python video_face_swap.py --video input.mp4 --source face.jpg --workers 3

  # Multiple source images (averaged to create a composite reference face)
  python video_face_swap.py --video input.mp4 --source face1.jpg face2.jpg face3.jpg --workers 4

  # Process only a specific time range
  python video_face_swap.py --video input.mp4 --source face.jpg \\
      --workers 4 --start-time 00:01:00 --end-time 00:02:30

  # Specify where to put the work directory
  python video_face_swap.py --video input.mp4 --source face.jpg \\
      --workers 2 --work-base /data/results

  # Explicit output path (overrides default inside work dir)
  python video_face_swap.py --video input.mp4 --source face.jpg \\
      --output /somewhere/result.mp4
"""
    )

    parser.add_argument('--video', required=True, help='Input video file path')
    parser.add_argument('--source', required=True, nargs='+', help='Source face image path(s). Multiple images will be averaged to create a composite reference face.')
    parser.add_argument('--output', default=None, help='Output video file path (default: inside work dir)')
    parser.add_argument('--workers', type=int, default=2, help='Number of parallel FaceFusion processes (default: 2)')
    parser.add_argument('--start-time', default=None, help='Start time (HH:MM:SS or seconds)')
    parser.add_argument('--end-time', default=None, help='End time (HH:MM:SS or seconds)')
    parser.add_argument('--work-base', default='.', help='Parent directory for the work folder (default: current dir)')
    parser.add_argument('--config-path', default='facefusion.ini', help='Path to FaceFusion config file (default: facefusion.ini)')
    parser.add_argument('--batch-size', type=int, default=300, help='Frames per mini-batch inside each worker (default: 300)')
    parser.add_argument('--turbo-mode', action='store_true', help='Turbo mode: extract only keyframes and skip video reassembly for quick preview')
    parser.add_argument('--face-swap-debug', action='store_true', help='Enable face swap debug prints in FaceFusion workers')

    args = parser.parse_args()

    # Validate inputs
    if not os.path.isfile(args.video):
        print(f'[Error] Video file not found: {args.video}')
        sys.exit(1)
    for src in args.source:
        if not os.path.isfile(src):
            print(f'[Error] Source face image not found: {src}')
            sys.exit(1)
    if not os.path.isfile(args.config_path):
        print(f'[Error] Config file not found: {args.config_path}')
        sys.exit(1)

    # Find facefusion.py script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    facefusion_script = os.path.join(script_dir, 'facefusion.py')
    if not os.path.isfile(facefusion_script):
        print(f'[Error] facefusion.py not found at: {facefusion_script}')
        sys.exit(1)

    if args.workers < 1:
        print('[Error] --workers must be >= 1')
        sys.exit(1)

    # Build work dir name: {video_stem}_{source_stems_joined}
    video_stem = os.path.splitext(os.path.basename(args.video))[0]
    source_stems = [os.path.splitext(os.path.basename(s))[0] for s in args.source]
    source_stem = '_'.join(source_stems) if len(source_stems) <= 3 else source_stems[0] + f'_+{len(source_stems)-1}more'
    work_dir_name = f'{video_stem}_{source_stem}'
    work_dir = os.path.abspath(os.path.join(args.work_base, work_dir_name))
    os.makedirs(work_dir, exist_ok=True)

    # Output defaults to inside work dir
    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        video_ext = os.path.splitext(os.path.basename(args.video))[1] or '.mp4'
        output_path = os.path.join(work_dir, f'{video_stem}_{source_stem}_output{video_ext}')

    print('=' * 70)
    print('  Video Face-Swap Pipeline')
    print('=' * 70)
    print(f'  Video:        {args.video}')
    print(f'  Source face:  {", ".join(args.source)}')
    print(f'  Output:       {output_path}')
    print(f'  Workers:      {args.workers}')
    print(f'  Batch size:   {args.batch_size}')
    print(f'  Time range:   {args.start_time or "start"} ~ {args.end_time or "end"}')
    print(f'  Work dir:     {work_dir}')
    print(f'  Config:       {args.config_path}')
    print('=' * 70)

    start_total = time.time()

    # Step 1: Extract frames
    frames_dir, audio_path, fps = extract_frames(
        args.video, work_dir, args.start_time, args.end_time
    )

    # Step 1.5: Detect keyframes and collect originals
    keyframe_indices = detect_keyframe_indices(args.video, args.start_time, args.end_time)
    _, keyframe_names = collect_keyframes(frames_dir, work_dir, keyframe_indices)

    # Step 2: Split into segments
    segments = split_frames_into_segments(frames_dir, work_dir, args.workers)

    # Step 3: Launch workers
    processes = launch_workers(segments, args.source, args.config_path, facefusion_script, work_dir, args.batch_size, face_swap_debug=args.face_swap_debug)

    # Step 4: Monitor progress
    print(f'[Step 4] Monitoring progress ...')

    # Give workers a moment to start, then check if any died immediately
    time.sleep(3)
    for proc in processes:
        ret = proc.poll()
        if ret is not None and ret != 0:
            proc._log_file.flush()
            log_path = os.path.join(work_dir, f'worker_{proc._worker_id}.log')
            print(f'\n[ERROR] Worker {proc._worker_id} exited immediately with code {ret}!')
            print(f'--- worker_{proc._worker_id}.log ---')
            try:
                with open(log_path, 'r') as f:
                    content = f.read()
                    if content.strip():
                        print(content)
                    else:
                        print('  (log is empty)')
            except Exception:
                print('  (could not read log)')
            print('---')

    monitor_progress(segments, processes)

    # Close log files and check return codes
    failed_workers = []
    for proc in processes:
        proc.wait()
        if hasattr(proc, '_log_file'):
            proc._log_file.close()
        if proc.returncode != 0:
            failed_workers.append(proc._worker_id)

    if failed_workers:
        print(f'[Warning] Workers {failed_workers} exited with errors. Check logs in {work_dir}/')
        # Print tail of error logs
        for wid in failed_workers:
            log_path = os.path.join(work_dir, f'worker_{wid}.log')
            if os.path.exists(log_path):
                print(f'\n--- Last 20 lines of worker_{wid}.log ---')
                with open(log_path, 'r') as f:
                    lines = f.readlines()
                    for line in lines[-20:]:
                        print(f'  {line}', end='')
                print()

    # Count processed frames
    processed = sum(
        len(glob(os.path.join(seg['output_dir'], 'frame_*.png')))
        for seg in segments
    )
    expected = sum(seg['frame_count'] for seg in segments)
    print(f'[Info] Processed {processed}/{expected} frames')

    if processed == 0:
        print('[Error] No frames were processed! Check worker logs.')
        sys.exit(1)

    # Step 4.5: Collect swapped keyframes (zero extra computation)
    collect_swapped_keyframes(segments, work_dir, keyframe_names)

    # Step 5: Reassemble
    reassemble_video(segments, output_path, fps, audio_path, work_dir)

    elapsed = time.time() - start_total
    print()
    print('=' * 70)
    print(f'  Done! Total time: {elapsed:.1f}s ({elapsed/60:.1f} min)')
    print(f'  Output:           {output_path}')
    print(f'  Intermediate dir: {work_dir} (preserved)')
    print(f'  Keyframes:        {len(keyframe_names)} frames')
    print(f'    Original:       {work_dir}/keyframes_original/')
    print(f'    Swapped:        {work_dir}/keyframes_swapped/')
    print('=' * 70)


if __name__ == '__main__':
    main()
