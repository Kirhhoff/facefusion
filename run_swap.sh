#!/bin/bash
#
# Quick-run wrapper for video_face_swap.py
#
# Usage:
#   ./run_swap.sh <video> <start_time> <end_time>
#
# Example:
#   ./run_swap.sh ../v/259.mp4 00:49:00 00:50:00
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ $# -lt 3 ]; then
    echo "Usage: $0 <video> <start_time> <end_time>"
    echo ""
    echo "  video       Input video file path"
    echo "  start_time  Start time in HH:MM:SS format"
    echo "  end_time    End time in HH:MM:SS format"
    echo ""
    echo "Example:"
    echo "  $0 ../v/259.mp4 00:49:00 00:50:00"
    exit 1
fi

VIDEO="$1"
START_TIME="$2"
END_TIME="$3"

python3 "${SCRIPT_DIR}/video_face_swap.py" \
    --config-path ./facefusion.ini \
    --work-base ../output/ \
    --source ../com/2_1.jpg \
    --video "$VIDEO" \
    --start-time "$START_TIME" \
    --end-time "$END_TIME" \
    --workers 48 \
    --batch-size 320
