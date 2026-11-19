"""Calibrate the C270 from multiple raw images of the 12-marker ArUco board."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def board_points(side=41.5, horizontal_gap=8, vertical_gap=9):
    horizontal_pitch = side + horizontal_gap
    vertical_pitch = side + vertical_gap
    square = np.array([[0, 0, 0], [side, 0, 0],
                       [side, side, 0], [0, side, 0]], dtype=np.float32)
    return {
        row * 4 + col + 1: square + np.array(
            [col * horizontal_pitch, row * vertical_pitch, 0], dtype=np.float32)
        for row in range(3) for col in range(4)
    }


def board_detector():
    aruco = cv2.aruco
    params = aruco.DetectorParameters()
    params.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX
    params.errorCorrectionRate = 0
    return aruco.ArucoDetector(
        aruco.getPredefinedDictionary(aruco.DICT_6X6_50), params)


def detect_board(frame, detector, board):
    corners, ids, _ = detector.detectMarkers(frame)
    markers = {}
    for marker_id, pixels in zip([] if ids is None else ids.ravel(), corners):
        if marker_id in board:
            if marker_id in markers:
                return {}
            markers[marker_id] = pixels.reshape(4, 2)
    return markers


def collect_views(paths):
    detector = board_detector()
    board = board_points()
    views = []
    size = None
    for path in paths:
        gray = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise ValueError(f'Cannot decode {path}')
        frame_size = gray.shape[::-1]
        if size is not None and frame_size != size:
            raise ValueError(f'{path}: expected frame size {size}, got {frame_size}')
        size = frame_size
        markers = detect_board(gray, detector, board)
        if len(markers) != len(board):
            print(f'{path.name}: skipped, detected {len(markers)}/12 board markers')
            continue
        pixels = np.concatenate([markers[marker_id] for marker_id in board])
        if any(np.mean(np.linalg.norm(pixels - view[1], axis=1)) < 3 for view in views):
            print(f'{path.name}: skipped, nearly identical to an earlier view')
            continue
        views.append((path, pixels))
        print(f'{path.name}: accepted 12/12 markers')
    return size, np.concatenate(list(board.values())), views


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', type=Path, nargs='+', help='Original C270 JPEG or PNG frames')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path)
    mode.add_argument('--check-last', action='store_true',
                      help='Check that the last frame adds a distinct view')
    args = parser.parse_args()
    try:
        size, points, views = collect_views(args.images)
        if args.check_last:
            return 0 if views and views[-1][0] == args.images[-1] else 1
        if len(views) < 12:
            raise ValueError(f'Need 12 distinct board views, found {len(views)}')
        images = [pixels for _, pixels in views]
        rms, matrix, distortion, rotations, translations = cv2.calibrateCamera(
            [points] * len(views), images, size, None, None)
        if not np.isfinite(matrix).all() or not np.isfinite(distortion).all() or not np.isfinite(rms):
            raise ValueError('Calibration returned non-finite parameters')
        errors = {}
        for (path, pixels), rotation, translation in zip(views, rotations, translations):
            projected, _ = cv2.projectPoints(points, rotation, translation, matrix, distortion)
            errors[str(path)] = float(np.sqrt(np.mean(np.sum(
                (projected.reshape(-1, 2) - pixels) ** 2, axis=1))))
        result = {
            'schema_version': 1,
            'source': 'Logitech C270, ArUco 6x6 board, IDs 1-12',
            'model': 'opencv_pinhole',
            'image_width': size[0],
            'image_height': size[1],
            'camera_matrix': matrix.tolist(),
            'distortion': distortion.ravel().tolist(),
            'alpha': 0,
            'rms_reprojection_px': rms,
            'per_view_reprojection_px': errors,
            'metric_depth_validated': False,
        }
        with args.output.open('x', encoding='utf-8') as file:
            json.dump(result, file, indent=2)
        print(f'{args.output}: {len(views)} views, reprojection RMS {rms:.3f} px')
    except (OSError, ValueError, cv2.error) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
