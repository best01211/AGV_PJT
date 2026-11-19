"""Save one 640x480 C270 image pair for stationary stereo calibration."""
import argparse
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

import cv2


def capture(left, right):
    cameras = []
    try:
        for device in (left, right):
            camera = cv2.VideoCapture(device, cv2.CAP_V4L2)
            cameras.append(camera)
            if not camera.isOpened():
                raise RuntimeError(f'Cannot open {device}')
            camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        # Grab both buffers before decoding either image.
        for _ in range(4):
            grab_times = []
            for device, camera in zip((left, right), cameras):
                if not camera.grab():
                    raise RuntimeError(f'Cannot grab a frame from {device}')
                grab_times.append(time.monotonic())
        frames = []
        for device, camera in zip((left, right), cameras):
            ok, frame = camera.retrieve()
            if not ok or frame.shape[:2] != (480, 640):
                raise RuntimeError(f'Cannot decode a 640x480 frame from {device}')
            frames.append(frame)
        return frames, (grab_times[1] - grab_times[0]) * 1000
    finally:
        for camera in cameras:
            camera.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--left', required=True)
    parser.add_argument('--right', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if Path(args.left).resolve() == Path(args.right).resolve():
        parser.error('Left and right must identify different devices')
    try:
        frames, gap_ms = capture(args.left, args.right)
        print(save_pair(args.output, frames, args.left, args.right, gap_ms))
    except (OSError, RuntimeError, cv2.error) as error:
        parser.exit(1, f'{error}\n')


def save_pair(output, frames, left, right, gap_ms):
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'pair-{uuid4().hex}'
    with tempfile.TemporaryDirectory(dir=output) as directory:
        staging = Path(directory) / 'pair'
        staging.mkdir()
        for side, frame in zip(('left', 'right'), frames):
            if not cv2.imwrite(str(staging / f'{side}.jpg'), frame,
                               [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise RuntimeError(f'Cannot save {side} image')
        metadata = {'left_device': left, 'right_device': right, 'grab_completion_gap_ms': gap_ms}
        (staging / 'capture.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        staging.rename(target)
    return target


if __name__ == '__main__':
    main()
