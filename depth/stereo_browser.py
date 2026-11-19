"""Receive JETANK stereo pairs and serve a local depth validation page."""
import argparse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import cv2
import numpy as np


def common_preview_crop(valid):
    """Find the largest rectangle containing only valid pixels in both views."""
    heights = np.zeros(valid.shape[1], dtype=np.int32)
    best = (0, 0, 0, 0)
    for row, mask in enumerate(valid):
        heights = np.where(mask, heights + 1, 0)
        stack = []
        for col, height in enumerate([*map(int, heights), 0]):
            start = col
            while stack and stack[-1][1] > height:
                start, previous_height = stack.pop()
                width = col - start
                if width * previous_height > best[2] * best[3]:
                    best = (start, row + 1 - previous_height, width, previous_height)
            if height and (not stack or stack[-1][1] < height):
                stack.append((start, height))
    if not best[2] or not best[3]:
        raise ValueError('Rectified cameras have no shared preview area')
    return best


class StereoDepth:
    def __init__(self, calibration, block_size=5, foundation_matcher=None, rectify_alpha=0):
        self.foundation_matcher = foundation_matcher
        self.metric_scale = float(calibration.get('metric_scale', 1.0))
        if not np.isfinite(self.metric_scale) or self.metric_scale <= 0:
            raise ValueError('metric_scale must be finite and positive')
        size = tuple(calibration['image_size'])
        keys = ('left_camera_matrix', 'left_distortion', 'right_camera_matrix',
                'right_distortion', 'rotation', 'translation_mm')
        kl, dl, kr, dr, rotation, translation = [
            np.asarray(calibration[key], dtype=np.float64) for key in keys]
        rl, rr, pl, pr, self.q, _, _ = cv2.stereoRectify(
            kl, dl, kr, dr, size, rotation, translation.reshape(3, 1),
            flags=cv2.CALIB_ZERO_DISPARITY, alpha=rectify_alpha)
        self.maps = [cv2.initUndistortRectifyMap(k, d, r, p, size, cv2.CV_16SC2)
                     for k, d, r, p in ((kl, dl, rl, pl), (kr, dr, rr, pr))]
        ones = np.ones(size[::-1], dtype=np.uint8)
        self.valid = [cv2.remap(ones, *maps, cv2.INTER_NEAREST) > 0 for maps in self.maps]
        if rectify_alpha:
            coverage = [cv2.remap(ones * 255, *maps, cv2.INTER_LINEAR) == 255
                        for maps in self.maps]
            self.preview_crop = common_preview_crop(coverage[0] & coverage[1])
        else:
            self.preview_crop = (0, 0, *size)
        self.rows, self.cols = np.indices(size[::-1])
        self.matchers = [cv2.StereoSGBM_create(
            minDisparity=minimum, numDisparities=256, blockSize=block_size,
            P1=8 * block_size**2, P2=32 * block_size**2,
            uniquenessRatio=10, speckleWindowSize=60, speckleRange=2, disp12MaxDiff=1,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY) for minimum in (0, -255)]

    def rectify(self, originals):
        frames = [cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) for jpeg in originals]
        if any(frame is None or frame.shape[:2] != self.rows.shape for frame in frames):
            raise ValueError('좌우 원본의 해상도가 보정 파일과 다릅니다.')
        return [cv2.remap(frame, *maps, cv2.INTER_LINEAR)
                for frame, maps in zip(frames, self.maps)]

    def measure(self, originals, x, y):
        left, right = self.rectify(originals)
        if self.foundation_matcher is None:
            lgray, rgray = [cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) for frame in (left, right)]
            disparity = self.matchers[0].compute(lgray, rgray).astype(np.float32) / 16
            reverse = self.matchers[1].compute(rgray, lgray).astype(np.float32) / 16
            reverse_valid = reverse > -256
        else:
            disparity, reverse = self.foundation_matcher(left, right)
            reverse_valid = np.isfinite(reverse) & (reverse < 0)
        disparity = np.where(np.isfinite(disparity), disparity, 0)
        width = disparity.shape[1]
        projected = np.rint(self.cols - disparity).astype(np.int32)
        clipped = projected.clip(0, width - 1)
        other = reverse[self.rows, clipped]
        valid = (self.valid[0] & self.valid[1][self.rows, clipped] &
                 (projected >= 0) & (projected < width) & (disparity > 0) &
                 reverse_valid[self.rows, clipped] & (np.abs(disparity + other) <= 1.5))
        xyz = cv2.reprojectImageTo3D(disparity, self.q) * self.metric_scale
        z = xyz[:, :, 2]
        valid &= np.isfinite(xyz).all(axis=2) & (z > 0) & (z < 3000)
        distance = np.zeros_like(z)
        distance[valid] = np.linalg.norm(xyz[valid], axis=1) / 10
        colors = cv2.applyColorMap(np.uint8(np.clip((120 - distance) * 2.55, 0, 255)), cv2.COLORMAP_JET)
        colors[~valid] = 0
        region = np.s_[y-10:y+11, x-10:x+11]
        use = valid[region]
        coverage = float(use.mean())
        result = {'x': x, 'y': y, 'valid_fraction': coverage, 'range_cm': None,
                  'matcher': 'FoundationStereo' if self.foundation_matcher else 'SGBM',
                  'metric_scale': self.metric_scale}
        if coverage >= .5:
            result['range_cm'] = float(np.median(distance[region][use]))
            result['depth_z_cm'] = float(np.median(z[region][use]) / 10)
        for frame in (left, colors):
            cv2.rectangle(frame, (x-10, y-10), (x+10, y+10), (255, 255, 255), 1)
        images = [base64.b64encode(cv2.imencode('.jpg', frame)[1]).decode('ascii')
                  for frame in (left, colors, right)]
        return result, images


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--calibration', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--calibration-label', help='Label shown above the camera comparison')
    parser.add_argument('--rectify-alpha', type=float, default=0,
                        help='Balance border cropping and retained field of view (0–1)')
    parser.add_argument('--aruco-captures', type=Path, help='Enable saving CSI ArUco calibration pairs')
    parser.add_argument('--block-size', type=int, choices=range(5, 24, 2), default=5,
                        help='Stereo matching window width in pixels')
    parser.add_argument('--foundation-repo', type=Path)
    parser.add_argument('--foundation-checkpoint', type=Path)
    parser.add_argument('--foundation-iterations', type=int, default=16)
    parser.add_argument('--foundation-input-scale', type=float, default=1.0)
    args = parser.parse_args()
    if args.foundation_iterations < 1:
        parser.error('--foundation-iterations must be positive')
    if not 0.25 <= args.foundation_input_scale <= 1:
        parser.error('--foundation-input-scale must be between 0.25 and 1')
    if not 0 <= args.rectify_alpha <= 1:
        parser.error('--rectify-alpha must be between 0 and 1')
    cv2.setNumThreads(4)
    if bool(args.foundation_repo) != bool(args.foundation_checkpoint):
        parser.error('Provide both --foundation-repo and --foundation-checkpoint')
    matcher = None
    if args.foundation_repo:
        from depth.foundation_stereo import FoundationMatcher
        matcher = FoundationMatcher(args.foundation_repo, args.foundation_checkpoint,
                                    iterations=args.foundation_iterations,
                                    input_scale=args.foundation_input_scale)
    engine = StereoDepth(json.loads(args.calibration.read_text(encoding='utf-8')),
                         block_size=args.block_size, foundation_matcher=matcher,
                         rectify_alpha=args.rectify_alpha)
    page = (Path(__file__).resolve().parents[1] / 'frontend' / 'stereo.html').read_bytes()
    height, width = engine.rows.shape
    cache = OrderedDict()
    capture_lock = threading.Lock()
    inference_lock = threading.Lock()
    latest_frame = None
    cache_lock = threading.Lock()
    board_lock = threading.Lock()
    if args.aruco_captures:
        from depth.calibrate_c270 import board_detector, board_points, detect_board
        from depth.calibrate_csi_stereo import collect_pairs
        board = board_points()
        detector = board_detector()
        _, board_views = collect_pairs(args.aruco_captures, (14, 9), board)

    class Handler(BaseHTTPRequestHandler):
        def send_data(self, code, data, kind='application/json'):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, code, value):
            self.send_data(code, json.dumps(value).encode('utf-8'))

        def do_GET(self):
            request = urlsplit(self.path)
            if request.path == '/':
                self.send_data(200, page, 'text/html; charset=utf-8')
            elif request.path == '/preview':
                try:
                    originals, _, _ = self.read_pair()
                    shift = parse_qs(request.query).get('vertical')
                    if shift is None:
                        frames = engine.rectify(originals)
                        x, y, w, h = engine.preview_crop
                        frames = [frame[y:y+h, x:x+w] for frame in frames]
                    else:
                        dy = int(shift[0])
                        if abs(dy) >= height:
                            raise ValueError('Vertical shift exceeds image height')
                        frames = [cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                                  for jpeg in originals]
                        if any(frame is None or frame.shape[:2] != (height, width) for frame in frames):
                            raise ValueError('Unexpected source image size')
                        x, y, w, h = 0, 0, width, height
                        transform = np.array([[1, 0, 0], [0, 1, dy]], dtype=np.float32)
                        frames[0] = cv2.warpAffine(
                            frames[0], transform, (width, height), flags=cv2.INTER_NEAREST,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(72, 61, 52))
                    images = [base64.b64encode(cv2.imencode('.jpg', frame)[1]).decode('ascii')
                              for frame in frames]
                    self.send_json(200, dict(left=images[0], right=images[1],
                                             raw_left=base64.b64encode(originals[0]).decode('ascii'),
                                             raw_right=base64.b64encode(originals[1]).decode('ascii'),
                                             aruco_capture=bool(args.aruco_captures),
                                             preview_crop=(x, y, w, h), image_size=(width, height),
                                             calibration_label=args.calibration_label))
                except (OSError, ValueError, KeyError, TypeError, cv2.error):
                    self.send_json(503, {'error': 'JETANK 영상 연결 끊김 · 재연결 중'})
            else:
                self.send_json(404, {'error': '페이지가 없습니다.'})

        def do_POST(self):
            try:
                if self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('JSON 입력이 필요합니다.')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 2048:
                    raise ValueError('입력 크기를 확인해 주세요.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('입력 형식을 확인해 주세요.')
                if self.path == '/frame':
                    self.send_json(200, self.frame(body))
                elif self.path == '/save':
                    self.send_json(200, self.save(body))
                elif self.path == '/capture-board' and args.aruco_captures:
                    self.send_json(200, self.capture_board())
                else:
                    self.send_json(404, {'error': '요청을 찾을 수 없습니다.'})
            except (KeyError, ValueError, TypeError) as error:
                self.send_json(422, {'error': str(error)})
            except (OSError, cv2.error):
                self.send_json(503, {'error': 'JETANK 연결 또는 프레임 처리를 확인해 주세요.'})

        def read_pair(self):
            with capture_lock:
                with urlopen(Request(args.source), timeout=2) as response:
                    split = int(response.headers['X-Left-Length'])
                    pair_id = response.headers['X-Pair-Id']
                    grab_gap_ms = float(response.headers['X-Grab-Gap-Ms'])
                    payload = response.read(2_000_001)
                if not 0 < split < len(payload) <= 2_000_000:
                    raise ValueError('좌우 프레임 묶음이 손상됐습니다.')
                return (payload[:split], payload[split:]), pair_id, grab_gap_ms

        def frame(self, body):
            nonlocal latest_frame
            x, y = int(body['x']), int(body['y'])
            if not (10 <= x < width-10 and 10 <= y < height-10):
                raise ValueError('표적 좌표가 영상 범위를 벗어났습니다.')
            began = time.perf_counter()
            with inference_lock:
                if latest_frame is not None:
                    completed_at, previous = latest_frame
                    if completed_at >= began and (previous['x'], previous['y']) == (x, y):
                        return previous
                fetch_started = time.perf_counter()
                originals, pair_id, grab_gap_ms = self.read_pair()
                fetched = time.perf_counter()
                result, images = engine.measure(originals, x, y)
                finished = time.perf_counter()
                token = uuid4().hex
                result.update(pair_id=pair_id, grab_gap_ms=grab_gap_ms,
                              elapsed_ms=round((finished-began)*1000),
                              receive_ms=round((fetched-fetch_started)*1000),
                              compute_ms=round((finished-fetched)*1000))
                with cache_lock:
                    cache[token] = (originals, result)
                    while len(cache) > 24:
                        cache.popitem(last=False)
                frame = dict(result, token=token, left=images[0], depth=images[1], right=images[2],
                             raw_left=base64.b64encode(originals[0]).decode('ascii'),
                             raw_right=base64.b64encode(originals[1]).decode('ascii'),
                             calibration_label=args.calibration_label)
                latest_frame = (finished, frame)
                return frame

        def capture_board(self):
            with board_lock:
                originals, _, _ = self.read_pair()
                frames = [cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
                          for jpeg in originals]
                if any(frame is None or frame.shape != engine.rows.shape for frame in frames):
                    raise ValueError('좌우 영상 해상도를 확인해 주세요.')
                detected = [detect_board(frame, detector, board) for frame in frames]
                counts = [len(markers) for markers in detected]
                result = dict(markers=counts, total=len(board_views))
                if any(count != len(board) for count in counts):
                    return dict(result, status='incomplete')
                left, right = [np.concatenate([markers[i] for i in board]).reshape(-1, 1, 2)
                               for markers in detected]
                if any(np.mean(np.linalg.norm(left - old_left, axis=2)) < 3
                       for old_left, _ in board_views):
                    return dict(result, status='duplicate')
                args.aruco_captures.mkdir(parents=True, exist_ok=True)
                destination = args.aruco_captures / ('pair-' + uuid4().hex)
                with tempfile.TemporaryDirectory(dir=args.aruco_captures) as temporary:
                    staged = Path(temporary) / 'pair'
                    staged.mkdir()
                    for side, jpeg in zip(('left', 'right'), originals):
                        (staged / (side + '.jpg')).write_bytes(jpeg)
                    staged.rename(destination)
                board_views.append((left, right))
                return dict(result, status='saved', total=len(board_views), folder=destination.name)

        def save(self, body):
            actual = float(body['actual_cm'])
            if not 0 < actual <= 300:
                raise ValueError('실측 거리(cm)를 입력해 주세요.')
            with cache_lock:
                item = cache.get(body['token'])
            if item is None:
                raise ValueError('프레임이 만료됐습니다. 화면을 재생해 다시 선택하세요.')
            originals, result = item
            estimated = result['range_cm']
            record = dict(result, actual_cm=actual,
                          error_cm=None if estimated is None else estimated-actual)
            args.output.mkdir(parents=True, exist_ok=True)
            target = args.output / ('measurement-' + uuid4().hex)
            with tempfile.TemporaryDirectory(dir=args.output) as temporary:
                staged = Path(temporary) / target.name
                staged.mkdir()
                for side, jpeg in zip(('left', 'right'), originals):
                    (staged / (side+'.jpg')).write_bytes(jpeg)
                (staged / 'measurement.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
                staged.rename(target)
            return {'folder': target.name, 'error_cm': record['error_cm']}

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'http://127.0.0.1:{args.port}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
