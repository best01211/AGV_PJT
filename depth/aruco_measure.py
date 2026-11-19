"""Measure a printed ArUco marker in an original, calibrated camera frame."""
import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np

from depth.processing import Undistorter


def measure(frame, calibration, marker_mm, dictionary, marker_id):
    if (frame.shape[1], frame.shape[0]) != calibration.size:
        raise ValueError(f'Frame size must match calibration {calibration.size}')
    if not math.isfinite(marker_mm) or marker_mm <= 0:
        raise ValueError('Marker side length must be positive and finite')
    if not dictionary.startswith('DICT_') or not hasattr(cv2.aruco, dictionary):
        raise ValueError(f'Unknown ArUco dictionary {dictionary}')
    aruco_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    corners, ids, _ = cv2.aruco.ArucoDetector(aruco_dict, params).detectMarkers(frame)
    matches = [] if ids is None else np.flatnonzero(ids.ravel() == marker_id)
    if len(matches) != 1:
        raise ValueError(f'Expected one visible marker with ID {marker_id}, found {len(matches)}')

    pixels = corners[matches[0]].reshape(4, 2).astype(np.float64)
    half = marker_mm / 2000
    points = np.array([[-half, half, 0], [half, half, 0],
                       [half, -half, 0], [-half, -half, 0]])
    _, rotations, translations, _ = cv2.solvePnPGeneric(
        points, pixels, calibration.K, calibration.D, flags=cv2.SOLVEPNP_IPPE_SQUARE)
    candidates = []
    for rotation, translation in zip(rotations, translations):
        matrix, _ = cv2.Rodrigues(rotation)
        camera_points = points @ matrix.T + translation.ravel()
        if not np.isfinite(camera_points).all() or np.any(camera_points[:, 2] <= 0):
            continue
        projected, _ = cv2.projectPoints(points, rotation, translation, calibration.K, calibration.D)
        residuals = projected.reshape(4, 2) - pixels
        rms = float(np.sqrt(np.mean(np.sum(residuals ** 2, axis=1))))
        candidates.append((rms, translation.ravel()))
    if not candidates:
        raise ValueError('No valid marker pose in front of the camera')
    candidates.sort(key=lambda candidate: candidate[0])
    rms, center = candidates[0]
    return {
        'marker_id': marker_id,
        'marker_side_mm': marker_mm,
        'depth_z_m': float(center[2]),
        'ray_range_m': float(np.linalg.norm(center)),
        'reprojection_rms_px': rms,
        'alternate_reprojection_rms_px': candidates[1][0] if len(candidates) > 1 else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path, help='Original camera image before undistortion')
    parser.add_argument('--calibration', type=Path, required=True)
    parser.add_argument('--marker-mm', type=float, required=True, help='Measured outer black square side')
    parser.add_argument('--marker-id', type=int, required=True)
    parser.add_argument('--dictionary', default='DICT_4X4_50')
    args = parser.parse_args()
    try:
        frame = cv2.imdecode(np.frombuffer(args.image.read_bytes(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if frame is None:
            raise ValueError('Could not decode image')
        result = measure(frame, Undistorter(args.calibration), args.marker_mm,
                         args.dictionary, args.marker_id)
    except (OSError, ValueError, KeyError, cv2.error) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
