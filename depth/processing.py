import json
from pathlib import Path

import cv2
import numpy as np


class Undistorter:
    def __init__(self, path):
        self.config = json.loads(Path(path).read_text(encoding='utf-8'))
        c = self.config
        if c.get('model') != 'opencv_pinhole' or c.get('alpha') != 0:
            raise ValueError('Unsupported calibration model or alpha')
        self.size = (int(c['image_width']), int(c['image_height']))
        self.K = np.asarray(c['camera_matrix'], dtype=np.float64)
        self.D = np.asarray(c['distortion'], dtype=np.float64)
        if self.K.shape != (3, 3) or self.D.shape != (5,) or not np.isfinite(self.K).all() or not np.isfinite(self.D).all():
            raise ValueError('Invalid calibration parameters')
        if self.K[0, 0] <= 0 or self.K[1, 1] <= 0:
            raise ValueError('Invalid focal length')
        self.new_K, self.roi = cv2.getOptimalNewCameraMatrix(self.K, self.D, self.size, 0, self.size)
        self.map_x, self.map_y = cv2.initUndistortRectifyMap(self.K, self.D, None, self.new_K, self.size, cv2.CV_32FC1)
        valid = np.full((self.size[1], self.size[0]), 255, dtype=np.uint8)
        self.mask = cv2.remap(valid, self.map_x, self.map_y, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT) == 255

    def apply(self, frame):
        if frame is None or (frame.shape[1], frame.shape[0]) != self.size:
            raise ValueError(f'Camera calibration requires {self.size[0]}x{self.size[1]} input')
        return cv2.remap(frame, self.map_x, self.map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)


def summarize_depth(depth, valid_mask):
    if depth.shape != valid_mask.shape:
        raise ValueError('Depth and valid mask shapes differ')
    h, w = depth.shape
    x0, y0 = w // 2 - 10, h // 2 - 10
    region = depth[y0:y0+20, x0:x0+20]
    mask = valid_mask[y0:y0+20, x0:x0+20] & np.isfinite(region) & (region > 0) & (region < 20)
    values = region[mask]
    return {'roi': [x0, y0, 20, 20], 'valid_pixels': int(values.size),
            'model_center_m': float(np.median(values)) if values.size >= 200 else None,
            'metric_validated': False, 'usable_for_control': False,
            'warning': 'Model estimate only; PDF reports cross-scene distance-order inversions. No one-point scaling applied.'}


def colorize_depth(depth, valid_mask, display_max_m=3):
    if display_max_m<=0:
        raise ValueError('Invalid display range')
    valid = valid_mask & np.isfinite(depth) & (depth > 0) & (depth < 20)
    safe = np.where(valid, depth, 20)
    # Fixed close-range display scale. Values >=3m share a color; numeric model output is unchanged.
    image = cv2.applyColorMap(np.uint8(np.clip(1 - safe / display_max_m, 0, 1) * 255), cv2.COLORMAP_TURBO)
    image[~valid] = [96, 96, 96]
    return image
