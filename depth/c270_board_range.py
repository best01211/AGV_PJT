"""Measure the range to marker 6 using all corners on the C270 board."""
import json
from pathlib import Path
import sys

import cv2
import numpy as np

from depth.calibrate_c270 import board_detector, board_points, detect_board
from depth.processing import Undistorter


def measure(frame, calibration):
    if frame is None or frame.shape[::-1] != calibration.size:
        return {'error': 'frame_size'}
    board = board_points()
    markers = detect_board(frame, board_detector(), board)
    if len(markers) != len(board):
        return {'error': 'markers', 'count': len(markers)}
    points = np.concatenate(list(board.values()))
    pixels = np.concatenate([markers[marker_id] for marker_id in board])
    ok, rotation, translation = cv2.solvePnP(points, pixels, calibration.K, calibration.D)
    if not ok:
        return {'error': 'pose'}
    matrix, _ = cv2.Rodrigues(rotation)
    camera_points = points @ matrix.T + translation.ravel()
    if not np.isfinite(camera_points).all() or np.any(camera_points[:, 2] <= 0):
        return {'error': 'pose'}
    center = matrix @ board[6].mean(axis=0) + translation.ravel()
    return {'range_cm': float(np.linalg.norm(center) / 10), 'depth_cm': float(center[2] / 10)}


if __name__ == '__main__':
    calibration = Undistorter(Path(sys.argv[1]))
    frame = cv2.imdecode(np.frombuffer(sys.stdin.buffer.read(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    print(json.dumps(measure(frame, calibration)))
