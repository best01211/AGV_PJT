"""Fit the stereo browser calibration from matched CSI calibration-board JPEGs."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from depth.calibrate_c270 import board_detector, board_points, detect_board


def collect_pairs(root, pattern, aruco_board=None):
    observations = []
    size = None
    detector = board_detector() if aruco_board is not None else None
    for left in sorted(root.glob('*/left.jpg')):
        right = left.with_name('right.jpg')
        frames = [cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
                  for path in (left, right)]
        if any(frame is None for frame in frames):
            raise ValueError(f'Cannot decode {left.parent}')
        size = size or frames[0].shape[::-1]
        if any(frame.shape[::-1] != size for frame in frames):
            raise ValueError(f'Inconsistent image size in {left.parent}')
        if detector is None:
            detections = [cv2.findChessboardCornersSB(frame, pattern) for frame in frames]
        else:
            detections = []
            for frame in frames:
                markers = detect_board(frame, detector, aruco_board)
                complete = len(markers) == len(aruco_board)
                corners = (np.concatenate([markers[i] for i in aruco_board]).reshape(-1, 1, 2)
                           if complete else None)
                detections.append((complete, corners))
        if not all(found for found, _ in detections):
            print(f'{left.parent.name}: full board missing in one or both frames')
            continue
        lpoints, rpoints = [points for _, points in detections]
        # A checkerboard can be indexed from either end. The two parallel
        # forward-facing cameras see its first row in the same direction.
        if detector is None:
            end = pattern[0] - 1
            if np.dot(lpoints[end, 0] - lpoints[0, 0],
                      rpoints[end, 0] - rpoints[0, 0]) < 0:
                rpoints = rpoints[::-1].copy()
        if any(np.mean(np.linalg.norm(lpoints - old, axis=2)) < 3
               for old, _ in observations):
            print(f'{left.parent.name}: pose too close to an earlier pair')
            continue
        observations.append((lpoints, rpoints))
        print(f'{left.parent.name}: accepted')
    return size, observations


def vertical_alignment(calibration, observations):
    keys = ('left_camera_matrix', 'left_distortion', 'right_camera_matrix',
            'right_distortion', 'rotation', 'translation_mm')
    kl, dl, kr, dr, rotation, translation = [
        np.asarray(calibration[key], dtype=np.float64) for key in keys]
    rl, rr, pl, pr, _, _, _ = cv2.stereoRectify(
        kl, dl, kr, dr, tuple(calibration['image_size']), rotation, translation.reshape(3, 1),
        flags=cv2.CALIB_ZERO_DISPARITY, alpha=0)
    raw, rectified = [], []
    for lp, rp in observations:
        raw.extend(np.abs(lp[:, 0, 1] - rp[:, 0, 1]))
        ly = cv2.undistortPoints(lp, kl, dl, R=rl, P=pl)[:, 0, 1]
        ry = cv2.undistortPoints(rp, kr, dr, R=rr, P=pr)[:, 0, 1]
        rectified.extend(np.abs(ly - ry))
    return dict(pairs=len(observations), raw_vertical_p95_px=float(np.percentile(raw, 95)),
                rectified_vertical_p95_px=float(np.percentile(rectified, 95)))


def fit(size, observations, pattern, square_mm, aruco_board=None):
    if aruco_board is None:
        grid = np.zeros((pattern[0] * pattern[1], 3), dtype=np.float32)
        grid[:, :2] = np.mgrid[:pattern[0], :pattern[1]].T.reshape(-1, 2) * square_mm
        geometry = dict(checkerboard_inner_corners=pattern, square_mm=square_mm)
    else:
        grid = np.concatenate(list(aruco_board.values()))
        geometry = dict(aruco_dictionary='DICT_6X6_50', aruco_ids=list(aruco_board),
                        board_size_mm=np.ptp(grid[:, :2], axis=0).tolist(), marker_side_mm=square_mm)
    objects = [grid] * len(observations)
    left, right = map(list, zip(*observations))
    left_rms, kl, dl, _, _ = cv2.calibrateCamera(objects, left, size, None, None)
    right_rms, kr, dr, _, _ = cv2.calibrateCamera(objects, right, size, None, None)
    rms, kl, dl, kr, dr, rotation, translation, _, _ = cv2.stereoCalibrate(
        objects, left, right, kl, dl, kr, dr, size, flags=cv2.CALIB_FIX_INTRINSIC)
    if not all(np.isfinite(value).all() for value in (kl, dl, kr, dr, rotation, translation, rms)):
        raise ValueError('Calibration returned non-finite parameters')
    if translation[0, 0] >= 0 or abs(translation[0, 0]) <= abs(translation[1, 0]):
        raise ValueError('Expected horizontally mounted cameras in physical left/right order')
    result = dict(image_size=size, **geometry,
                accepted_pairs=len(observations), left_camera_matrix=kl.tolist(),
                left_distortion=dl.ravel().tolist(), right_camera_matrix=kr.tolist(),
                right_distortion=dr.ravel().tolist(), rotation=rotation.tolist(),
                translation_mm=translation.ravel().tolist(), metric_scale=1.0,
                left_rms_px=left_rms, right_rms_px=right_rms, stereo_rms_px=rms)
    result['fit_rectified_vertical_p95_px'] = vertical_alignment(
        result, observations)['rectified_vertical_p95_px']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pairs', type=Path, help='Directory of pair folders with left.jpg and right.jpg')
    parser.add_argument('--square-mm', type=float, required=True)
    parser.add_argument('--aruco-size-mm', nargs=2, type=float, metavar=('WIDTH', 'HEIGHT'),
                        help='Use the 4x3 ArUco board. Set --square-mm to the marker edge length.')
    parser.add_argument('--validation-pairs', type=Path, help='Separate checkerboard pairs for alignment verification')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not np.isfinite(args.square_mm) or args.square_mm <= 0:
        parser.error('--square-mm must be finite and positive')
    pattern = (14, 9)
    aruco_board = None
    if args.aruco_size_mm:
        width, height = args.aruco_size_mm
        side = args.square_mm
        if not np.isfinite([width, height]).all() or width <= 4 * side or height <= 3 * side:
            parser.error('ArUco dimensions must include positive gaps between markers')
        aruco_board = board_points(side, (width - 4 * side) / 3, (height - 3 * side) / 2)
    try:
        size, observations = collect_pairs(args.pairs, pattern, aruco_board)
        if len(observations) < 12:
            raise ValueError(f'Need at least 12 distinct paired views, found {len(observations)}')
        result = fit(size, observations, pattern, args.square_mm, aruco_board)
        if args.validation_pairs is not None:
            if args.validation_pairs.resolve() == args.pairs.resolve():
                raise ValueError('Validation pairs must be separate from fitting pairs')
            validation_size, validation = collect_pairs(args.validation_pairs, pattern, aruco_board)
            if not validation or validation_size != size:
                raise ValueError('Need at least one complete validation pair at the calibration resolution')
            result['validation_alignment'] = alignment = vertical_alignment(result, validation)
            print(f"Validation vertical p95 {alignment['raw_vertical_p95_px']:.3f} px "
                  f"before and {alignment['rectified_vertical_p95_px']:.3f} px after rectification")
        with args.output.open('x', encoding='utf-8') as output:
            json.dump(result, output, indent=2)
        print(f"Stereo RMS {result['stereo_rms_px']:.3f} px, "
              f"rectified vertical p95 {result['fit_rectified_vertical_p95_px']:.3f} px")
    except (ValueError, OSError, cv2.error) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
