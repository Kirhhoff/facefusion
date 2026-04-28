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
import re
import subprocess
import sys
import time
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
    Obtain **all** step-level args with their proper defaults by leveraging
    FaceFusion's own ``create_program().parse_args()``.

    The root cause of the ``'NoneType' object has no attribute 'get'`` /
    ``'NoneType' object is not subscriptable`` errors is that
    ``apply_args(step_args, state_manager.set_item)`` calls
    ``args.get('some_key')`` for **every** step key.  If the key is missing
    from ``step_args``, ``dict.get()`` returns ``None``, and
    ``state_manager.set_item`` **overwrites** the default that was set by
    ``init_item`` during CLI parsing.

    Simply reading non-empty values from ``facefusion.ini`` is insufficient
    because many INI keys are intentionally left empty (meaning "use the
    built-in default"), and those empty keys would be skipped — leaving the
    step_args dict incomplete.

    The fix: call FaceFusion's own ``create_program().parse_args()`` with a
    minimal argument list.  This triggers the full argparse pipeline which:
      1. Reads ``facefusion.ini`` via the ``config.get_*`` helpers
      2. Falls back to hard-coded defaults for empty INI entries
      3. Returns a complete namespace with **every** key populated

    We then filter down to step-level keys via ``reduce_step_args`` and
    return the result.  This way, *every* step key has a proper value — no
    ``None`` overwrites, and no maintenance of key/type lists is needed.

    Parameters
    ----------
    config_path : str
        Path to ``facefusion.ini`` (used as ``--config-path`` for argparse).

    Returns
    -------
    dict
        Complete step args dict with all keys populated.
    """
    # Late import so this module can be loaded without the facefusion
    # package on PYTHONPATH during development / testing.
    from facefusion.program import create_program
    from facefusion.args import reduce_step_args

    # Parse with minimal args — argparse fills in every default, pulling
    # from facefusion.ini for non-empty entries and using hard-coded
    # fallbacks for empty ones.
    program = create_program()
    known_args, _ = program.parse_known_args([
        'headless-run',
        '--config-path', config_path,
        '--source-paths', '__placeholder__',
        '--target-path',   '__placeholder__',
        '--output-path',   '__placeholder__',
    ])
    args_dict = vars(known_args)

    # Filter to step-level keys only
    step_args = reduce_step_args(args_dict)

    # Remove the keys we set manually per-step so they don't collide.
    # (source_paths / target_path / output_path contain placeholder values.)
    for _key in ('source_paths', 'target_path', 'output_path', 'processors'):
        step_args.pop(_key, None)

    return step_args


# ---------------------------------------------------------------------------
# Step 1: Extract frames & audio
# ---------------------------------------------------------------------------

def extract_frames(video_path: str, work_dir: str, start_time: str = None, end_time: str = None, turbo_mode: bool = False) -> tuple:
    """
    Extract video frames as JPEG images and audio track.

    In normal mode: extracts ALL frames to ``frames/``.
    In turbo mode:  extracts ONLY keyframes (I-frames) to ``keyframes_original/``.

    Returns
    -------
    tuple
        Normal mode:  (frames_dir, audio_path_or_None, fps)
        Turbo mode:   (keyframes_original_dir, None, fps, keyframe_count, total_frame_count)

        The turbo-mode return has 5 elements so callers can distinguish
        the two cases via ``len(result)``.
    """
    fps = get_video_fps(video_path)
    print(f'[Info] Detected video FPS: {fps:.3f}')

    if turbo_mode:
        # ---- Turbo mode: extract only keyframes (I-frames) ----
        keyframes_dir = os.path.join(work_dir, 'keyframes_original')
        os.makedirs(keyframes_dir, exist_ok=True)

        # Use ffmpeg select filter to extract only I-frames
        # The select filter evaluates for each frame; eq(pict_type\,I) keeps I-frames
        cmd = ['ffmpeg', '-y']
        if start_time:
            cmd += ['-ss', start_time]
        if end_time:
            cmd += ['-to', end_time]
        cmd += [
            '-i', video_path,
            '-vf', "select=eq(pict_type\\,I)",
            '-vsync', 'vfr',
            '-qmin', '1', '-q:v', '1',  # high quality PNG
            os.path.join(keyframes_dir, 'frame_%08d.png')
        ]
        print(f'[Step 1] [Turbo Mode] Extracting keyframes (I-frames) ...')
        run_cmd(cmd)

        keyframe_count = len(glob(os.path.join(keyframes_dir, 'frame_*.png')))
        print(f'[Step 1] [Turbo Mode] Extracted {keyframe_count} keyframes')

        if keyframe_count == 0:
            print('[Warning] No keyframes extracted! The video may not contain I-frames '
                  'or the specified time range may have no keyframes.')
            if start_time or end_time:
                print(f'  Time range: {start_time or "start"} ~ {end_time or "end"}')
            print('[Info] Try adjusting the time range or check the video file.')
            # Return with empty result so caller can handle gracefully
            return keyframes_dir, None, fps, 0, 0

        # Compute total frame count in the specified range for statistics
        total_frame_count = _count_frames_in_range(video_path, start_time, end_time)
        if total_frame_count == 0:
            total_frame_count = keyframe_count  # fallback

        pct = 100 * keyframe_count / max(total_frame_count, 1)
        print(f'[Step 1] [Turbo Mode] Keyframes represent {pct:.1f}% of {total_frame_count} total frames')

        return keyframes_dir, None, fps, keyframe_count, total_frame_count

    else:
        # ---- Normal mode: extract ALL frames ----
        frames_dir = os.path.join(work_dir, 'frames')
        os.makedirs(frames_dir, exist_ok=True)

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


def _count_frames_in_range(video_path: str, start_time: str = None, end_time: str = None) -> int:
    """Count total number of video frames in the specified time range.

    Uses a fast two-step approach instead of ``-show_frames`` (which decodes
    every frame and is extremely slow for long videos):

    1. If the container reports ``nb_frames`` for the video stream, use that
       directly (instant, no decoding).
    2. Otherwise fall back to ``-show_packets`` (packet headers only, no
       decoding) and count packets whose pts_time falls within the range.

    When no time range is specified and ``nb_frames`` is available, the
    result is returned immediately without any additional ffprobe call.
    """
    start_sec = time_to_seconds(start_time) if start_time else None
    end_sec = time_to_seconds(end_time) if end_time else None

    # Fast path: try to get nb_frames from stream metadata (no decoding)
    try:
        out = run_cmd([
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=nb_frames,duration',
            '-of', 'json',
            video_path
        ], capture=True)
        info = json.loads(out)
        stream = info.get('streams', [{}])[0]
        nb_frames_str = stream.get('nb_frames', '')

        if nb_frames_str and not start_time and not end_time:
            # No time range — nb_frames is the exact answer
            return int(nb_frames_str)

        # If we have nb_frames and a time range, estimate by proportion
        if nb_frames_str:
            total_frames = int(nb_frames_str)
            duration_str = stream.get('duration', '')
            if duration_str:
                try:
                    total_duration = float(duration_str)
                except (ValueError, TypeError):
                    total_duration = get_video_duration(video_path)
            else:
                total_duration = get_video_duration(video_path)

            if total_duration > 0:
                range_start = start_sec if start_sec is not None else 0.0
                range_end = end_sec if end_sec is not None else total_duration
                range_duration = range_end - range_start
                if range_duration <= 0:
                    return 0
                # Proportional estimate
                return max(1, round(total_frames * range_duration / total_duration))
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError, KeyError):
        pass

    # Fallback: count video packets (fast — no decoding, just container parsing)
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_packets',
        '-show_entries', 'packet=pts_time',
        '-of', 'json',
        video_path,
    ]

    try:
        out = run_cmd(cmd, capture=True)
        if not out:
            return 0
        probe_data = json.loads(out)
        packets = probe_data.get('packets', [])
        count = 0
        for pkt in packets:
            pts_time_str = pkt.get('pts_time', '')
            try:
                pts_time = float(pts_time_str)
            except (ValueError, TypeError):
                continue
            if start_sec is not None and pts_time < start_sec:
                continue
            if end_sec is not None and pts_time > end_sec:
                continue
            count += 1
        return count
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return 0


# ---------------------------------------------------------------------------
# Step 1.5: Detect keyframes and create symlink directories
# ---------------------------------------------------------------------------

def detect_keyframe_indices(video_path: str, start_time: str = None, end_time: str = None) -> tuple:
    """
    Use ffprobe to detect which frames are keyframes (I-frames).

    Strategy: use ``-show_packets`` instead of ``-show_frames``.
    ``-show_frames`` decodes every frame which is extremely slow for long
    videos (can take hours for a 2-hour 1080p HEVC file).  ``-show_packets``
    only reads container-level packet metadata — no decoding required — and
    is typically 50-100× faster.

    Key-frames are identified by the ``K`` flag in the packet's ``flags``
    field (``flags=K_`` means key-frame).

    Frame-index alignment
    ---------------------
    Packets are enumerated in decode order starting from 1 so that the
    indices align with the extracted frame numbering
    (``frame_00000001.png`` is index 1).

    Returns
    -------
    tuple of (set, dict)
        - keyframe_indices : set of 1-based frame indices.
        - keyframe_info : dict mapping 1-based index → pts_time (float seconds)
          for every keyframe.
    """
    # Use -show_packets with compact output for fast parsing
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_packets',
        '-show_entries', 'packet=pts_time,flags',
        '-of', 'json',
        video_path,
    ]

    try:
        out = run_cmd(cmd, capture=True)
    except subprocess.CalledProcessError as e:
        print(f'[Warning] ffprobe command failed: {" ".join(cmd)}')
        print(f'  Error: {e}')
        return set(), {}

    if not out:
        print('[Warning] ffprobe returned empty output — no keyframe information available')
        return set(), {}

    try:
        probe_data = json.loads(out)
    except json.JSONDecodeError as e:
        print(f'[Warning] ffprobe output is not valid JSON: {e}')
        return set(), {}

    packets = probe_data.get('packets', [])
    if not packets:
        print('[Warning] ffprobe returned no packet entries — cannot detect keyframes')
        return set(), {}

    # Determine time-range boundaries (in seconds)
    start_sec = time_to_seconds(start_time) if start_time else None
    end_sec = time_to_seconds(end_time) if end_time else None

    keyframe_indices = set()
    keyframe_info = {}

    for i, pkt in enumerate(packets, start=1):
        flags = pkt.get('flags', '')
        pts_time_str = pkt.get('pts_time', '')

        # Key-frame packets have 'K' in their flags field
        if 'K' not in flags:
            continue

        # Parse pts_time — skip packets with invalid/missing timestamps
        try:
            pts_time = float(pts_time_str)
        except (ValueError, TypeError):
            continue

        # Filter by time range
        if start_sec is not None and pts_time < start_sec:
            continue
        if end_sec is not None and pts_time > end_sec:
            continue

        keyframe_indices.add(i)
        keyframe_info[i] = pts_time

    if not keyframe_indices:
        print('[Warning] No keyframes (I-frames) detected in the video')
        if start_sec is not None or end_sec is not None:
            print(f'  Time range: {start_time or "start"} ~ {end_time or "end"}')
    else:
        print(f'[Step 1.5] ffprobe detected {len(keyframe_indices)} keyframes in video')

    return keyframe_indices, keyframe_info


def collect_keyframes(frames_dir: str, work_dir: str, keyframe_indices: set,
                      keyframe_info: dict = None, video_path: str = None,
                      start_time: str = None, end_time: str = None, fps: float = None):
    """
    Create keyframes_original/ with symlinks to original keyframes.
    Returns (keyframes_original_dir, list_of_keyframe_names).

    Frame-index alignment
    ---------------------
    ``keyframe_indices`` are 1-based indices into the *source video* (as
    reported by ffprobe).  However, when ``start_time`` is specified the
    extracted frames in ``frames_dir`` start from ``frame_00000001.png``,
    which corresponds to the first frame *within the time range*, not the
    first frame of the video.  We must therefore convert video-frame indices
    to extracted-frame indices.

    Two strategies are attempted (in order):

    1. **Timestamp matching** (preferred): if ``keyframe_info`` (a mapping
       of index → pts_time) and ``fps`` are available, compute the expected
       pts_time for each extracted frame and match against keyframe
       timestamps.
    2. **Offset subtraction** (fallback): if the start offset in frames can
       be computed from ``start_time`` and ``fps``, simply subtract it from
       each keyframe index.
    """
    keyframes_orig_dir = os.path.join(work_dir, 'keyframes_original')
    os.makedirs(keyframes_orig_dir, exist_ok=True)

    all_frames = sorted(glob(os.path.join(frames_dir, 'frame_*.png')))
    total_extracted = len(all_frames)

    if total_extracted == 0:
        print('[Step 1.5] No frames found in frames_dir — skipping keyframe collection')
        return keyframes_orig_dir, []

    # Determine the frame offset (1-based video frame index of the first
    # extracted frame).
    start_sec = time_to_seconds(start_time) if start_time else 0.0

    # Strategy 1: timestamp matching using keyframe_info
    aligned_indices = set()
    if keyframe_info and fps and fps > 0:
        # Build a mapping: extracted-frame 1-based index → pts_time
        # The first extracted frame corresponds to start_sec.
        for idx in keyframe_indices:
            kf_pts = keyframe_info.get(idx)
            if kf_pts is None:
                continue
            # Compute which extracted frame this corresponds to
            # extracted frame 1 is at start_sec, frame 2 is at start_sec + 1/fps, etc.
            frame_offset = (kf_pts - start_sec) * fps
            extracted_idx = round(frame_offset) + 1  # 1-based
            if 1 <= extracted_idx <= total_extracted:
                aligned_indices.add(extracted_idx)
        if aligned_indices:
            print(f'[Step 1.5] Timestamp-matched {len(aligned_indices)} keyframes from {len(keyframe_indices)} video keyframes')

    # Strategy 2: offset subtraction fallback
    if not aligned_indices and start_sec > 0 and fps and fps > 0:
        offset_frames = round(start_sec * fps)
        for idx in keyframe_indices:
            extracted_idx = idx - offset_frames
            if 1 <= extracted_idx <= total_extracted:
                aligned_indices.add(extracted_idx)
        if aligned_indices:
            print(f'[Step 1.5] Offset-aligned {len(aligned_indices)} keyframes')

    # No offset needed (start from beginning) or no keyframe_info
    if not aligned_indices:
        # Direct index mapping (works when start_time is None / 0)
        aligned_indices = {idx for idx in keyframe_indices if 1 <= idx <= total_extracted}

    # Create symlinks
    keyframe_names = []
    for i, frame_path in enumerate(all_frames, start=1):
        if i in aligned_indices:
            name = os.path.basename(frame_path)
            link_path = os.path.join(keyframes_orig_dir, name)
            if not os.path.exists(link_path):
                os.symlink(os.path.abspath(frame_path), link_path)
            keyframe_names.append(name)

    pct = 100 * len(keyframe_names) / max(total_extracted, 1)
    print(f'[Step 1.5] Collected {len(keyframe_names)} keyframes out of {total_extracted} total frames ({pct:.1f}%)')
    print(f'  Original keyframes → {keyframes_orig_dir}')

    return keyframes_orig_dir, keyframe_names


def find_output_frame(output_dir: str, frame_name: str) -> str | None:
    """
    Find the actual output file for a given frame in the output directory.

    FaceFusion may leave processed frames with a temporary filename pattern
    (e.g. ``frame_00000001-worker-0-mb-0-0.png``) when the job is not fully
    finalized.  This function looks for both the original name and the
    temporary-name variant, preferring the original.

    Args:
        output_dir: The worker output directory to search in.
        frame_name: The original frame filename (e.g. ``frame_00000001.png``).

    Returns:
        The absolute path to the found file, or ``None`` if neither the
        original nor a temporary-name match exists.
    """
    # Priority 1: original filename
    original_path = os.path.join(output_dir, frame_name)
    if os.path.exists(original_path):
        return original_path

    # Priority 2: temporary filename  frame_XXXXXXXX-worker-*-mb-*.png
    # Extract the numeric part from the original frame name
    base = os.path.splitext(frame_name)[0]          # e.g. frame_00000001
    # The temporary pattern is: {base}-worker-{W}-mb-{M}-{S}.png
    pattern = re.compile(rf'^{re.escape(base)}-worker-\d+-mb-\d+-\d+\{os.path.splitext(frame_name)[1]}$')
    try:
        for entry in os.listdir(output_dir):
            if pattern.match(entry):
                return os.path.join(output_dir, entry)
    except OSError:
        pass

    return None


def collect_swapped_keyframes(segments: list, work_dir: str, keyframe_names: list):
    """
    After face-swapping, symlink the swapped versions of keyframes into keyframes_swapped/.
    Zero extra computation — just picks from existing output.
    Supports FaceFusion temporary filename pattern (frame_XXXXXXXX-worker-X-mb-X-X.png).
    """
    keyframes_swap_dir = os.path.join(work_dir, 'keyframes_swapped')
    os.makedirs(keyframes_swap_dir, exist_ok=True)

    keyframe_set = set(keyframe_names)
    found = 0
    temp_name_found = 0
    missing = []

    for seg in segments:
        for name in seg['frame_names']:
            if name in keyframe_set:
                result = find_output_frame(seg['output_dir'], name)
                link_path = os.path.join(keyframes_swap_dir, name)
                if result is not None:
                    if not os.path.exists(link_path):
                        os.symlink(os.path.abspath(result), link_path)
                    found += 1
                    if os.path.basename(result) != name:
                        temp_name_found += 1
                else:
                    missing.append(name)

    print(f'[Step 4.5] Collected {found}/{len(keyframe_names)} swapped keyframes → {keyframes_swap_dir}'
          f'  (temp-name matches: {temp_name_found})')
    if missing:
        print(f'  [Warning] {len(missing)} keyframes were not processed by face-swap: '
              f'{missing[:5]}{"..." if len(missing) > 5 else ""}')


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


def process_keyframes(keyframes_dir: str, source_paths: list, config_path: str,
                      facefusion_script: str, work_dir: str,
                      batch_size: int = 300, face_swap_debug: bool = False) -> str:
    """
    Run FaceFusion on keyframes only (turbo mode).

    This is a streamlined version of ``launch_workers`` that processes a
    single directory of keyframe images without splitting into segments.
    The output goes directly to ``keyframes_swapped/``.

    Parameters
    ----------
    keyframes_dir : str
        Directory containing keyframe images (from turbo-mode extraction).
    source_paths : list[str]
        Source face image path(s).
    config_path : str
        Path to facefusion.ini.
    facefusion_script : str
        Path to facefusion.py.
    work_dir : str
        Working directory.
    batch_size : int
        Frames per mini-batch (default 300).
    face_swap_debug : bool
        Enable debug output.

    Returns
    -------
    str
        Path to ``keyframes_swapped/`` directory.
    """
    keyframes_swapped_dir = os.path.join(work_dir, 'keyframes_swapped')
    os.makedirs(keyframes_swapped_dir, exist_ok=True)

    all_keyframes = sorted(glob(os.path.join(keyframes_dir, 'frame_*.png')))
    if not all_keyframes:
        print('[Error] No keyframe images found in ' + keyframes_dir)
        return keyframes_swapped_dir

    source_abs_list = [os.path.abspath(p) for p in source_paths]
    config_abs = os.path.abspath(config_path)
    facefusion_dir = os.path.dirname(os.path.abspath(facefusion_script))
    python_exe = sys.executable
    output_dir_abs = os.path.abspath(keyframes_swapped_dir)
    keyframes_dir_abs = os.path.abspath(keyframes_dir)

    # Read step-level config defaults
    config_step_defaults = _read_config_step_args(config_path)

    # Split keyframes into mini-batches
    frame_names = [os.path.basename(f) for f in all_keyframes]
    mini_batches = [frame_names[i:i + batch_size] for i in range(0, len(frame_names), batch_size)]

    # Build worker shell script
    script_path = os.path.join(work_dir, 'run_keyframe_worker.sh')
    with open(script_path, 'w') as f:
        f.write('#!/bin/bash\n')
        f.write(f'cd "{facefusion_dir}"\n\n')
        for mb_idx, mb_frames in enumerate(mini_batches):
            jobs_path = os.path.join(work_dir, f'jobs_keyframes_{mb_idx}')
            mb_job_id = f'keyframes-mb-{mb_idx}'

            # Build steps — each keyframe is one step
            steps = []
            for frame_name in mb_frames:
                frame_path = os.path.join(keyframes_dir_abs, frame_name)
                output_frame = os.path.join(output_dir_abs, frame_name)
                step_args = {
                    'source_paths': source_abs_list,
                    'target_path': frame_path,
                    'output_path': output_frame,
                    'processors': ['face_swapper'],
                }
                step_args.update(config_step_defaults)
                step = {
                    'args': step_args,
                    'status': 'queued',
                }
                if face_swap_debug:
                    step['args']['face_swap_debug'] = True
                steps.append(step)

            _write_facefusion_job_json(jobs_path, mb_job_id, steps)

            f.write(
                f'echo "[Keyframe Worker] mini-batch {mb_idx + 1}/{len(mini_batches)}'
                f' ({len(mb_frames)} keyframes)"\n'
            )
            f.write(
                f'"{python_exe}" "{facefusion_script}" job-run'
                f' {mb_job_id}'
                f' --config-path "{config_abs}"'
                f' --jobs-path "{jobs_path}"'
                '\n\n'
            )
    os.chmod(script_path, 0o755)

    # Run the worker script (single process — keyframes are few)
    print(f'[Step 2] [Turbo Mode] Processing {len(all_keyframes)} keyframes '
          f'in {len(mini_batches)} mini-batches ...')
    print(f'  Script:         {script_path}')
    print(f'  Source:         {", ".join(source_abs_list)}')
    print(f'  Input dir:      {keyframes_dir_abs}')
    print(f'  Output dir:     {output_dir_abs}')

    log_path = os.path.join(work_dir, 'keyframe_worker.log')

    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'

    log_file = open(log_path, 'w')
    proc = subprocess.Popen(
        ['bash', script_path],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=facefusion_dir,
        env=env,
    )

    # Monitor progress
    try:
        from tqdm import tqdm
        bar = tqdm(
            total=len(all_keyframes),
            desc='Keyframes',
            leave=True,
            ncols=100,
            colour='green',
            bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]'
        )
        prev_count = 0
        while proc.poll() is None:
            count = len(glob(os.path.join(keyframes_swapped_dir, 'frame_*.png')))
            delta = count - prev_count
            if delta > 0:
                bar.update(delta)
                prev_count = count
            time.sleep(1.0)
        # Final count
        count = len(glob(os.path.join(keyframes_swapped_dir, 'frame_*.png')))
        delta = count - prev_count
        if delta > 0:
            bar.update(delta)
        bar.close()
        print()
    except ImportError:
        while proc.poll() is None:
            count = len(glob(os.path.join(keyframes_swapped_dir, 'frame_*.png')))
            print(f'\r[Progress] Keyframes: {count}/{len(all_keyframes)}', end='', flush=True)
            time.sleep(2.0)
        print()

    # Check return code
    proc.wait()
    log_file.close()
    if proc.returncode != 0:
        print(f'[Warning] Keyframe worker exited with code {proc.returncode}. Check {log_path}')
        if os.path.exists(log_path):
            with open(log_path, 'r') as f:
                lines = f.readlines()
                print('--- Last 20 lines of keyframe_worker.log ---')
                for line in lines[-20:]:
                    print(f'  {line}', end='')
                print('---')

    processed = len(glob(os.path.join(keyframes_swapped_dir, 'frame_*.png')))
    print(f'[Step 2] [Turbo Mode] Processed {processed}/{len(all_keyframes)} keyframes')

    return keyframes_swapped_dir


def launch_workers(segments: list, source_paths: list, config_path: str, facefusion_script: str, work_dir: str, batch_size: int = 300, face_swap_debug: bool = False, worker_start_delay: float = 2.0) -> list:
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

        # Stagger worker launches to avoid GPU memory allocation spikes
        if worker_start_delay > 0 and seg != segments[-1]:
            print(f'  Waiting {worker_start_delay}s before launching next worker...')
            time.sleep(worker_start_delay)

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

def reassemble_video(segments: list, output_path: str, fps: float, audio_path: str = None, work_dir: str = None,
                     video_encoder: str = 'libx264', video_crf: int = 23, video_preset: str = 'medium'):
    """Collect all processed frames in order and encode back to video.

    Parameters
    ----------
    video_encoder : str
        Video encoder: ``libx264`` (default), ``mpeg4``, or ``h264_nvenc``.
    video_crf : int
        CRF value for libx264 (18-28, lower = better quality, default 23).
    video_preset : str
        Encoding preset for libx264 or h264_nvenc.
    """
    print('[Step 5] Reassembling video ...')

    # --- Diagnostic: show output directory contents ---
    for seg in segments:
        out_dir = seg['output_dir']
        if os.path.isdir(out_dir):
            entries = os.listdir(out_dir)
            original_count = sum(1 for e in entries if re.match(r'^frame_\d{8}\.png$', e))
            temp_count = sum(1 for e in entries if re.match(r'^frame_\d{8}-worker-\d+-mb-\d+-\d+\.png$', e))
            print(f'  [Diag] {out_dir}: {len(entries)} files '
                  f'(original-name: {original_count}, temp-name: {temp_count}, other: {len(entries) - original_count - temp_count})')

    # Collect all output frames in order
    ordered_dir = os.path.join(work_dir, 'ordered_output')
    os.makedirs(ordered_dir, exist_ok=True)

    frame_idx = 1
    swapped_count = 0       # frames successfully found in output (original or temp name)
    fallback_count = 0      # frames falling back to original (not processed)
    missing_count = 0       # frames completely missing
    temp_name_count = 0     # subset of swapped_count that used temp-name match

    for seg in segments:
        for name in seg['frame_names']:
            # Use find_output_frame to support FaceFusion temporary filename pattern
            result = find_output_frame(seg['output_dir'], name)
            if result is not None:
                src = result
                swapped_count += 1
                # Check if this was a temp-name match (for diagnostics)
                if os.path.basename(result) != name:
                    temp_name_count += 1
            else:
                # Fallback: use original frame from batch_dir (symlink -> frames/)
                original = os.path.join(seg['batch_dir'], name)
                if os.path.exists(original):
                    src = original
                    fallback_count += 1
                else:
                    print(f'[Warning] Frame missing: {name}')
                    missing_count += 1
                    continue

            # Create sequentially numbered symlink for ffmpeg
            link_name = f'frame_{frame_idx:08d}.png'
            link_path = os.path.join(ordered_dir, link_name)
            if not os.path.exists(link_path):
                os.symlink(os.path.abspath(src), link_path)
            frame_idx += 1

    total_count = swapped_count + fallback_count
    print(f'[Step 5] Frame matching summary:')
    print(f'  Total frames:        {total_count + missing_count}')
    print(f'  Swapped (output):    {swapped_count}  (temp-name matches: {temp_name_count})')
    print(f'  Fallback (original): {fallback_count}')
    print(f'  Missing:             {missing_count}')

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

    # Build video encoding arguments based on encoder choice
    if video_encoder == 'h264_nvenc':
        # NVIDIA hardware encoder: use -preset (p1-p7) and -cq (const quality)
        cmd += [
            '-c:v', 'h264_nvenc',
            '-preset', video_preset if video_preset.startswith('p') else 'p4',
            '-cq', str(video_crf),
            '-pix_fmt', 'yuv420p',
            output_path,
        ]
    elif video_encoder == 'mpeg4':
        cmd += [
            '-c:v', 'mpeg4',
            '-qscale:v', '2',
            '-pix_fmt', 'yuv420p',
            output_path,
        ]
    else:
        # Default: libx264 with CRF mode
        cmd += [
            '-c:v', 'libx264',
            '-crf', str(video_crf),
            '-preset', video_preset,
            '-pix_fmt', 'yuv420p',
            output_path,
        ]

    run_cmd(cmd)
    print(f'[Step 5] Output video saved to: {output_path} (encoder={video_encoder}, crf={video_crf}, preset={video_preset})')


# ---------------------------------------------------------------------------
# Turbo Mode Report
# ---------------------------------------------------------------------------

def _print_turbo_report(work_dir: str, keyframe_count: int, total_frame_count: int,
                        processed_keyframes: int, elapsed: float, args):
    """
    Print a summary report after turbo mode completes.
    Includes statistics, output paths, and a copy-paste command for full processing.
    """
    keyframes_original_dir = os.path.join(work_dir, 'keyframes_original')
    keyframes_swapped_dir = os.path.join(work_dir, 'keyframes_swapped')

    pct = 100 * keyframe_count / max(total_frame_count, 1)
    processed_pct = 100 * processed_keyframes / max(keyframe_count, 1)

    print()
    print('=' * 70)
    print('  TURBO MODE — Preview Complete')
    print('=' * 70)
    print(f'  Total time:           {elapsed:.1f}s ({elapsed/60:.1f} min)')
    print(f'  Total frames:         {total_frame_count}')
    print(f'  Keyframes (I-frames): {keyframe_count} ({pct:.1f}%)')
    print(f'  Processed keyframes:  {processed_keyframes}/{keyframe_count} ({processed_pct:.1f}%)')
    print()
    print('  Output directories:')
    print(f'    Original keyframes: {keyframes_original_dir}')
    print(f'    Swapped keyframes:  {keyframes_swapped_dir}')
    print()
    print('  To preview results, compare the directories above.')
    print('  To run FULL face-swap on all frames, use this command:')
    print()

    # Build the equivalent full-frame command (without --turbo-mode)
    cmd_parts = [
        sys.executable,
        os.path.abspath(__file__),
        '--video', args.video,
        '--source', *args.source,
        '--workers', str(args.workers),
        '--config-path', args.config_path,
        '--batch-size', str(args.batch_size),
    ]
    if args.start_time:
        cmd_parts += ['--start-time', args.start_time]
    if args.end_time:
        cmd_parts += ['--end-time', args.end_time]
    if args.work_base != '.':
        cmd_parts += ['--work-base', args.work_base]
    if args.output:
        cmd_parts += ['--output', args.output]
    if args.face_swap_debug:
        cmd_parts += ['--face-swap-debug']

    # Format the command nicely with line continuations
    cmd_str = ''
    for i, part in enumerate(cmd_parts):
        if i == 0:
            cmd_str = part
        elif part.startswith('-'):
            cmd_str += ' \\\n    ' + part
        else:
            cmd_str += ' ' + part

    print(f'    {cmd_str}')
    print()
    print(f'  Work directory: {work_dir} (preserved)')
    print('=' * 70)


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
    parser.add_argument('--frame-quality', type=int, default=95, help='JPEG quality for extracted frames (80-100, default: 95). Higher = better quality but larger files.')
    parser.add_argument('--video-encoder', default='libx264', choices=['libx264', 'mpeg4', 'h264_nvenc'], help='Video encoder for output (default: libx264). h264_nvenc requires NVIDIA GPU.')
    parser.add_argument('--video-crf', type=int, default=23, help='CRF value for libx264/h264_nvenc (18-28, default: 23). Lower = better quality but larger files.')
parser.add_argument('--video-preset', default='medium', help='Encoding preset for libx264 (ultrafast/superfast/veryfast/faster/fast/medium/slow/slower/veryslow) or h264_nvenc (p1-p7). Default: medium')
parser.add_argument('--worker-start-delay', type=float, default=2.0, help='Delay in seconds between launching each worker subprocess to avoid GPU memory allocation spikes (default: 2.0, set 0 to disable)')

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
    if args.turbo_mode:
        print('  ** TURBO MODE — keyframes only, no video output **')
    print('=' * 70)
    print(f'  Video:        {args.video}')
    print(f'  Source face:  {", ".join(args.source)}')
    print(f'  Output:       {output_path if not args.turbo_mode else "(not created in turbo mode)"}')
    print(f'  Workers:      {args.workers}')
    print(f'  Batch size:   {args.batch_size}')
    print(f'  Time range:   {args.start_time or "start"} ~ {args.end_time or "end"}')
    print(f'  Work dir:     {work_dir}')
    print(f'  Config:       {args.config_path}')
    print(f'  Turbo mode:   {args.turbo_mode}')
    print(f'  Frame quality: {args.frame_quality} (JPEG)')
    print(f'  Video encoder: {args.video_encoder} (CRF={args.video_crf}, preset={args.video_preset})')
    print('=' * 70)

    start_total = time.time()

    # ======================================================================
    # TURBO MODE
    # ======================================================================
    if args.turbo_mode:
        # Step 1 (turbo): Extract only keyframes
        result = extract_frames(
            args.video, work_dir, args.start_time, args.end_time,
            turbo_mode=True, frame_quality=args.frame_quality,
        )
        keyframes_dir, _, fps, keyframe_count, total_frame_count = result

        # If no keyframes were extracted, exit gracefully
        if keyframe_count == 0:
            print('[Info] Turbo mode finished with no keyframes to process.')
            return

        # Step 2 (turbo): Split keyframes into segments for parallel processing
        #    Reuse the same split logic as normal mode — all frames in
        #    keyframes_original/ are keyframes, so each worker gets a
        #    contiguous chunk.
        segments = split_frames_into_segments(keyframes_dir, work_dir, args.workers)

        # Step 3 (turbo): Launch parallel workers
        processes = launch_workers(
            segments, args.source, args.config_path,
            facefusion_script, work_dir, args.batch_size,
            face_swap_debug=args.face_swap_debug,
            worker_start_delay=args.worker_start_delay,
        )

        # Step 4 (turbo): Monitor progress
        print(f'[Step 4] [Turbo Mode] Monitoring {len(processes)} workers ...')

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
            for wid in failed_workers:
                log_path = os.path.join(work_dir, f'worker_{wid}.log')
                if os.path.exists(log_path):
                    print(f'\n--- Last 20 lines of worker_{wid}.log ---')
                    with open(log_path, 'r') as f:
                        lines = f.readlines()
                        for line in lines[-20:]:
                            print(f'  {line}', end='')
                    print()

        # Collect swapped keyframes from all worker output dirs into keyframes_swapped/
        all_keyframe_names = []
        for seg in segments:
            all_keyframe_names.extend(seg['frame_names'])
        collect_swapped_keyframes(segments, work_dir, all_keyframe_names)

        keyframes_swapped_dir = os.path.join(work_dir, 'keyframes_swapped')
        processed_keyframes = len(glob(os.path.join(keyframes_swapped_dir, 'frame_*.png')))

        # Print turbo mode summary report
        elapsed = time.time() - start_total
        _print_turbo_report(
            work_dir=work_dir,
            keyframe_count=keyframe_count,
            total_frame_count=total_frame_count,
            processed_keyframes=processed_keyframes,
            elapsed=elapsed,
            args=args,
        )
        return  # turbo mode done — no video reassembly

    # ======================================================================
    # NORMAL (FULL-FRAME) MODE
    # ======================================================================

    # Step 1: Extract frames
    frames_dir, audio_path, fps = extract_frames(
        args.video, work_dir, args.start_time, args.end_time,
        frame_quality=args.frame_quality,
    )

    # Step 1.5: Detect keyframes and collect originals
    keyframe_indices, keyframe_info = detect_keyframe_indices(args.video, args.start_time, args.end_time)
    _, keyframe_names = collect_keyframes(
        frames_dir, work_dir, keyframe_indices,
        keyframe_info=keyframe_info,
        video_path=args.video,
        start_time=args.start_time,
        end_time=args.end_time,
        fps=fps,
    )

    # Step 2: Split into segments
    segments = split_frames_into_segments(frames_dir, work_dir, args.workers)

    # Step 3: Launch workers
    processes = launch_workers(segments, args.source, args.config_path, facefusion_script, work_dir, args.batch_size, face_swap_debug=args.face_swap_debug, worker_start_delay=args.worker_start_delay)

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
    reassemble_video(segments, output_path, fps, audio_path, work_dir,
                                 video_encoder=args.video_encoder,
                                 video_crf=args.video_crf,
                                 video_preset=args.video_preset)

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
