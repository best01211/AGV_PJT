"""Beam uncertainty model. Pose must come from an external estimator, never Mock motion."""
import math
import threading
import time
from collections import deque

from mapping.models import MapConfig, ObservationBatch


class OccupancyGrid:
    def __init__(self, config: MapConfig):
        self.config = config
        self.cols = round(config.width_m / config.resolution_m)
        self.rows = round(config.height_m / config.resolution_m)
        self.log_odds = [0.0] * (self.cols * self.rows)
        self.observed = [False] * len(self.log_odds)
        self.revision = 0
        self.accepted = 0
        self.rejected = 0
        self.last_timestamps = {}
        self.last_pose = None
        self.pose_trail = deque(maxlen=1500)
        self.latest_ranges = {}
        self.last_update_s = None
        self.sources = set()
        self.lock = threading.RLock()

    def _inside(self, x, y):
        c = self.config
        return c.origin_x_m <= x < c.origin_x_m + c.width_m and c.origin_y_m <= y < c.origin_y_m + c.height_m

    def _pose_reason(self, pose, now):
        c = self.config
        if pose.frame_id != c.frame_id:
            return 'frame_mismatch'
        if not pose.valid or pose.confidence < c.min_pose_confidence:
            return 'unreliable_pose'
        if pose.position_std_m > c.max_pose_position_std_m or pose.yaw_std_rad > c.max_pose_yaw_std_rad:
            return 'pose_uncertainty_exceeded'
        if not self._inside(pose.x_m, pose.y_m):
            return 'pose_out_of_bounds'
        if now - pose.timestamp_s > c.max_age_s or pose.timestamp_s > now + 0.1:
            return 'invalid_pose_time'
        return None

    def integrate(self, batch: ObservationBatch, now=None):
        now = time.time() if now is None else now
        with self.lock:
            pose = batch.pose
            pose_reason = self._pose_reason(pose, now)
            mounts = {s.sensor_id: s for s in self.config.sensors}
            results = []
            for measurement in batch.measurements:
                mount = mounts.get(measurement.sensor_id)
                reason = pose_reason
                if not reason and mount is None:
                    reason = 'unknown_sensor'
                if not reason and (not measurement.valid or measurement.distance_m is None or measurement.return_type != 'echo'):
                    reason = 'invalid_measurement'
                if not reason and not mount.min_range_m <= measurement.distance_m <= mount.max_range_m:
                    reason = 'range_out_of_bounds'
                if not reason and (now - measurement.timestamp_s > self.config.max_age_s or measurement.timestamp_s > now + 0.1):
                    reason = 'invalid_measurement_time'
                if not reason and abs(pose.timestamp_s - measurement.timestamp_s) > self.config.max_sync_error_s:
                    reason = 'unsynchronized'
                if not reason and measurement.timestamp_s <= self.last_timestamps.get(measurement.sensor_id, 0):
                    reason = 'duplicate_or_out_of_order'
                if not reason:
                    cosine, sine = math.cos(pose.yaw_rad), math.sin(pose.yaw_rad)
                    sx = pose.x_m + cosine * mount.x_m - sine * mount.y_m
                    sy = pose.y_m + sine * mount.x_m + cosine * mount.y_m
                    if not self._inside(sx, sy):
                        reason = 'sensor_out_of_bounds'
                if reason:
                    self.rejected += 1
                    results.append({'sensor_id': measurement.sensor_id, 'accepted': False, 'reason': reason})
                    continue
                changed = self._beam(sx, sy, pose.yaw_rad + mount.yaw_rad,
                                     mount, measurement.distance_m, pose)
                self.last_timestamps[measurement.sensor_id] = measurement.timestamp_s
                self.sources.add(measurement.source)
                self.accepted += 1
                self.latest_ranges[measurement.sensor_id] = {
                    'distance_m': measurement.distance_m, 'timestamp_s': measurement.timestamp_s,
                    'pose': {'x_m': pose.x_m, 'y_m': pose.y_m, 'yaw_rad': pose.yaw_rad},
                    'source': measurement.source,
                }
                previous = self.last_pose
                if previous is None or pose.timestamp_s > previous['timestamp_s']:
                    self.last_pose = pose.model_dump()
                    self.pose_trail.append({
                        'x_m': pose.x_m, 'y_m': pose.y_m,
                        'break_before': previous is None or pose.timestamp_s - previous['timestamp_s'] > self.config.max_age_s,
                    })
                self.last_update_s = max(self.last_update_s or 0, measurement.timestamp_s)
                self.revision += 1
                results.append({'sensor_id': measurement.sensor_id, 'accepted': True, 'updated_cells': changed})
            return {'revision': self.revision, 'results': results}

    def _beam(self, sx, sy, heading, mount, distance, pose):
        c, resolution = self.config, self.config.resolution_m
        # Position error broadens radial uncertainty; yaw error broadens the angular support.
        sigma = math.hypot(mount.range_sigma_m, pose.position_std_m)
        band = max(resolution, 2 * sigma)
        half_angle = min(math.pi, mount.fov_rad / 2 + 2 * pose.yaw_std_rad)
        reach = distance + band
        x0 = max(0, math.floor((sx - reach - c.origin_x_m) / resolution))
        x1 = min(self.cols - 1, math.floor((sx + reach - c.origin_x_m) / resolution))
        y0 = max(0, math.floor((sy - reach - c.origin_y_m) / resolution))
        y1 = min(self.rows - 1, math.floor((sy + reach - c.origin_y_m) / resolution))
        changed = 0
        for row in range(y0, y1 + 1):
            for col in range(x0, x1 + 1):
                dx = c.origin_x_m + (col + .5) * resolution - sx
                dy = c.origin_y_m + (row + .5) * resolution - sy
                radius = math.hypot(dx, dy)
                if radius < mount.min_range_m or radius > reach:
                    continue
                angle = math.atan2(math.sin(math.atan2(dy, dx) - heading), math.cos(math.atan2(dy, dx) - heading))
                if abs(angle) > half_angle:
                    continue
                delta = 0
                if abs(radius - distance) <= band:
                    # Distributed, weak positive evidence across the return arc, not an exact hit point.
                    delta = .35 * math.exp(-.5 * ((radius - distance) / max(sigma, resolution / 2)) ** 2)
                elif c.enable_free_updates and radius < distance - band:
                    # Optional weak free evidence, only within the inner half of the nominal beam.
                    if abs(angle) < mount.fov_rad / 4 and radius > mount.min_range_m + pose.position_std_m:
                        delta = -.12
                if delta:
                    index = row * self.cols + col
                    self.log_odds[index] = max(-4, min(4, self.log_odds[index] + delta))
                    self.observed[index] = True
                    changed += 1
        return changed

    def snapshot(self):
        with self.lock:
            data = [-1 if not seen else round(100 / (1 + math.exp(-value)))
                    for value, seen in zip(self.log_odds, self.observed)]
            return {'schema_version': 1, 'config': self.config.model_dump(),
                    'cols': self.cols, 'rows': self.rows, 'revision': self.revision,
                    'data': data, 'accepted': self.accepted, 'rejected': self.rejected,
                    'last_pose': self.last_pose, 'last_update_s': self.last_update_s,
                    'pose_trail': list(self.pose_trail), 'latest_ranges': self.latest_ranges.copy(),
                    'sources': sorted(self.sources), 'localization': 'external_pose_required',
                    'usable_for_navigation': False,
                    'warning': 'Experimental beam evidence map; not a validated navigation map. Unknown cells are not free.'}

    def export(self):
        with self.lock:
            payload = self.snapshot()
            del payload['pose_trail'], payload['latest_ranges']
            return dict(payload, log_odds=self.log_odds.copy(), observed=self.observed.copy(),
                        last_timestamps=self.last_timestamps.copy())

    @classmethod
    def restore(cls, payload):
        if not isinstance(payload, dict) or payload.get('schema_version') != 1:
            raise ValueError('unsupported map schema')
        grid = cls(MapConfig.model_validate(payload['config']))
        odds, observed = payload['log_odds'], payload['observed']
        if len(odds) != len(grid.log_odds) or len(observed) != len(odds):
            raise ValueError('invalid grid size')
        if any(type(v) not in (int, float) or not math.isfinite(v) or not -4 <= v <= 4 for v in odds):
            raise ValueError('invalid log odds')
        if any(type(v) is not bool for v in observed):
            raise ValueError('invalid observed mask')
        grid.log_odds, grid.observed = odds.copy(), observed.copy()
        timestamps = payload.get('last_timestamps', {})
        sensor_ids = {s.sensor_id for s in grid.config.sensors}
        if not isinstance(timestamps, dict) or any(k not in sensor_ids or type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for k, v in timestamps.items()):
            raise ValueError('invalid timestamps')
        sources = payload.get('sources', [])
        if not isinstance(sources, list) or any(s not in ('hardware', 'recorded') for s in sources):
            raise ValueError('invalid sources')
        grid.last_timestamps = timestamps.copy()
        grid.sources = set(sources)
        revision = payload.get('revision', 0)
        if type(revision) is not int or revision < 0:
            raise ValueError('invalid revision')
        grid.revision = revision
        # A loaded map does not imply a live robot pose. New observations start a new session.
        return grid
