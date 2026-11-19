import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.main import app
from backend import main, mapping_api
from backend.sensor_navigation import Footprint, navigation_preview
from backend.service import RobotService
from mapping.models import MapConfig, ObservationBatch
from mapping.occupancy_grid import OccupancyGrid
from mapping.map_storage import MapStorage


def config(**changes):
    data = dict(frame_id='map', width_m=4, height_m=4, resolution_m=.1,
                sensors=[dict(sensor_id='front', x_m=0, y_m=0, yaw_rad=0,
                              fov_rad=.4, min_range_m=.05, max_range_m=3, range_sigma_m=.05)])
    data.update(changes)
    return MapConfig.model_validate(data)


def observation(timestamp=1000, **pose_changes):
    pose = dict(frame_id='map', timestamp_s=timestamp, x_m=1.05, y_m=1.05,
                yaw_rad=0, valid=True, confidence=.95, position_std_m=0,
                yaw_std_rad=0, source='external_measured')
    pose.update(pose_changes)
    return dict(pose=pose, measurements=[dict(sensor_id='front', timestamp_s=timestamp,
                distance_m=1, valid=True, source='hardware', return_type='echo')])


def synthetic_corridor():
    """Repeat a survey from known poses so the mapping model accumulates free evidence."""
    mounts = config().model_dump()['sensors']
    mounts[0]['fov_rad'] = .9
    grid = OccupancyGrid(config(sensors=mounts, enable_free_updates=True, origin_x_m=-1))
    for row in range(5, 35):
        for sample in range(12):
            timestamp = 1000 + row + sample / 20
            data = observation(timestamp, x_m=-.85, y_m=(row + .5) / 10)
            measurement = data['measurements'][0]
            measurement.update(distance_m=3, source='recorded')
            grid.integrate(ObservationBatch.model_validate(data), now=timestamp)
    return grid


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.grid = OccupancyGrid(config())

    def apply(self, data, now=1000):
        return self.grid.integrate(ObservationBatch.model_validate(data), now=now)['results'][0]

    def test_unknown_initial_and_no_implicit_free(self):
        self.assertEqual(set(self.grid.snapshot()['data']), {-1})
        self.assertTrue(self.apply(observation())['accepted'])
        self.assertGreater(self.grid.snapshot()['data'][10 * 40 + 20], 50)
        self.assertEqual(self.grid.snapshot()['data'][10 * 40 + 15], -1)
        self.assertFalse(self.grid.snapshot()['usable_for_navigation'])

    def test_beam_spreads_endpoint_evidence(self):
        self.apply(observation())
        self.assertTrue(self.grid.observed[11 * 40 + 20])
        self.assertFalse(self.grid.observed[15 * 40 + 20])

    def test_mount_and_robot_rotation(self):
        c = config()
        c.sensors[0].x_m = .2
        self.grid = OccupancyGrid(c)
        self.apply(observation(yaw_rad=math.pi / 2))
        self.assertGreater(self.grid.snapshot()['data'][22 * 40 + 10], 50)

    def test_invalid_pose_rejects_without_mutation(self):
        for changes in [dict(valid=False), dict(confidence=.2), dict(position_std_m=.5),
                        dict(yaw_std_rad=.5), dict(frame_id='other'), dict(x_m=-1),
                        dict(timestamp_s=998)]:
            self.assertFalse(self.apply(observation(**changes))['accepted'])
            self.assertEqual(self.grid.revision, 0)
            self.assertFalse(any(self.grid.observed))

    def test_measurement_rejections(self):
        for change in [dict(sensor_id='other'), dict(distance_m=None), dict(valid=False),
                       dict(distance_m=4), dict(timestamp_s=998), dict(timestamp_s=1000.3),
                       dict(timestamp_s=999.8), dict(return_type='no_echo')]:
            data = observation()
            data['measurements'][0].update(change)
            self.assertFalse(self.apply(data)['accepted'], change)
        self.assertFalse(any(self.grid.observed))

    def test_duplicate_and_out_of_order(self):
        self.apply(observation())
        before = self.grid.log_odds.copy()
        self.assertEqual(self.apply(observation())['reason'], 'duplicate_or_out_of_order')
        self.assertEqual(self.grid.log_odds, before)
        self.assertFalse(self.apply(observation(timestamp=999.9))['accepted'])

    def test_trail_uses_ordered_poses_and_breaks_after_a_gap(self):
        mounts = config().model_dump()['sensors']
        mounts.append({**mounts[0], 'sensor_id': 'rear', 'yaw_rad': math.pi})
        self.grid = OccupancyGrid(config(sensors=mounts))
        self.assertTrue(self.apply(observation())['accepted'])
        delayed = observation(timestamp=999.9)
        delayed['measurements'][0]['sensor_id'] = 'rear'
        self.assertTrue(self.apply(delayed)['accepted'])
        self.assertTrue(self.apply(observation(timestamp=1002, x_m=1.2), now=1002)['accepted'])
        self.assertTrue(self.apply(observation(timestamp=1002.1, x_m=1.3), now=1002.1)['accepted'])
        snapshot = self.grid.snapshot()
        self.assertEqual([point['break_before'] for point in snapshot['pose_trail']], [True, True, False])
        self.assertEqual(snapshot['last_pose']['timestamp_s'], 1002.1)
        self.assertEqual(snapshot['latest_ranges']['rear']['timestamp_s'], 999.9)

    def test_free_updates_are_opt_in_and_no_echo_never_clears(self):
        self.grid = OccupancyGrid(config(enable_free_updates=True))
        data = observation()
        data['measurements'][0]['return_type'] = 'no_echo'
        self.assertFalse(self.apply(data)['accepted'])
        self.assertFalse(any(self.grid.observed))
        self.apply(observation())
        self.assertLess(self.grid.snapshot()['data'][10 * 40 + 15], 50)

    def test_map_boundary_does_not_wrap(self):
        data = observation(x_m=3.95)
        self.apply(data)
        self.assertFalse(any(self.grid.observed))

    def test_probability_saturation(self):
        for i in range(100):
            timestamp = 1000 + i * .01
            self.apply(observation(timestamp), now=timestamp)
        self.assertLessEqual(max(self.grid.log_odds), 4)
        self.assertGreater(self.grid.snapshot()['data'][10 * 40 + 20], 65)

    def test_invalid_config_and_nonfinite_values(self):
        for changes in [dict(width_m=100, height_m=100, resolution_m=.02),
                        dict(width_m=4.05), dict(sensors=[]), dict(resolution_m=0)]:
            with self.assertRaises(ValidationError):
                config(**changes)
        data = observation(x_m=float('nan'))
        with self.assertRaises(ValidationError):
            ObservationBatch.model_validate(data)

    def test_storage_roundtrip_and_duplicate_protection(self):
        self.apply(observation())
        with tempfile.TemporaryDirectory() as directory:
            storage = MapStorage(directory)
            map_id = storage.save(self.grid)
            restored = storage.load(map_id)
            self.assertEqual(restored.snapshot()['data'], self.grid.snapshot()['data'])
            self.assertIsNone(restored.last_pose)
            self.assertEqual(restored.snapshot()['pose_trail'], [])
            self.assertEqual(restored.snapshot()['latest_ranges'], {})
            result = restored.integrate(ObservationBatch.model_validate(observation()), now=1000)
            self.assertFalse(result['results'][0]['accepted'])
            with self.assertRaises(ValueError):
                storage.load('../private')

    def test_corrupt_grid_rejected(self):
        payload = self.grid.export()
        payload['log_odds'][0] = float('inf')
        with self.assertRaises(ValueError):
            OccupancyGrid.restore(payload)

    def test_navigation_preview_keeps_blocked_evidence_and_reserves_clearance(self):
        grid = OccupancyGrid(config(width_m=1, height_m=1))
        grid.observed[:] = [True] * 100
        grid.log_odds[:] = [-2] * 100
        grid.observed[33] = False
        grid.log_odds[55] = 0
        grid.log_odds[77] = 2
        preview = navigation_preview(grid, Footprint(margin_m=0))
        for index, value in {
            0: 75, 11: 75, 27: 0, 33: -1, 22: 75, 55: 50, 44: 75, 77: 100, 66: 75,
        }.items():
            self.assertEqual(preview['data'][index], value, index)
        self.assertEqual(preview['free_evidence_cells'], 97)
        self.assertEqual(preview['clearance_cells'], 2)
        self.assertAlmostEqual(preview['clearance_radius_m'], math.sqrt(.02))
        self.assertLess(preview['driveable_cells'], preview['free_evidence_cells'])
        self.assertEqual(grid.snapshot()['data'][22], 12)

    def test_recording_cli_builds_loadable_map(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'config.json').write_text(config().model_dump_json(), encoding='utf-8')
            (folder / 'observations.jsonl').write_text(json.dumps(observation())+'\n', encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'mapping.build_recording',
                                     '--config', str(folder / 'config.json'),
                                     '--input', str(folder / 'observations.jsonl'),
                                     '--output-dir', str(folder / 'maps')],
                                    capture_output=True, text=True, check=True)
            summary = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(summary['accepted'], 1)
            loaded = MapStorage(folder / 'maps').load(summary['map_id'])
            self.assertEqual(loaded.sources, {'recorded'})
            self.assertTrue(any(loaded.observed))


class MappingApiTest(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(mapping_api, 'grid', None))
        self.enterContext(patch.object(mapping_api, 'storage', MapStorage(self.directory)))
        self.enterContext(patch.object(main, 'service', RobotService()))
        self.client = self.enterContext(TestClient(app))

    def test_mapping_lifecycle_and_storage(self):
        self.assertEqual(self.client.get('/api/mapping/state').status_code, 409)
        c = config().model_dump()
        self.assertEqual(self.client.post('/api/mapping/initialize', json=c).status_code, 200)
        self.assertEqual(self.client.post('/api/mapping/initialize', json=c).status_code, 409)
        data = observation(time.time())
        result = self.client.post('/api/mapping/observations', json=data).json()
        self.assertTrue(result['results'][0]['accepted'])
        state = self.client.get('/api/mapping/state').json()
        self.assertEqual(state['pose_trail'][0]['x_m'], data['pose']['x_m'])
        self.assertEqual(state['latest_ranges']['front']['distance_m'], 1)
        saved = self.client.post('/api/mapping/save').json()['map_id']
        before = self.client.get('/api/mapping/state').json()['data']
        self.client.post('/api/mapping/reset', json=c)
        self.assertEqual(self.client.post('/api/mapping/load/'+saved).json()['data'], before)
        self.assertEqual(self.client.post('/api/mapping/load/invalid').status_code, 422)
        self.assertEqual(self.client.get('/mapping').status_code, 200)

    def test_bad_inputs(self):
        self.assertEqual(self.client.post('/api/mapping/initialize', json={}).status_code, 422)
        c = config().model_dump()
        self.client.post('/api/mapping/initialize', json=c)
        data = observation(time.time())
        data['pose']['source'] = 'mock'
        self.assertEqual(self.client.post('/api/mapping/observations', json=data).status_code, 422)

    def test_saved_observations_become_a_driveable_mock_map(self):
        mapping_api.grid = synthetic_corridor()
        map_id = self.client.post('/api/mapping/save').json()['map_id']
        # The preview and registration both read the immutable saved version.
        self.client.post('/api/mapping/reset', json=config().model_dump())
        base = '/api/navigation/sensor-maps/' + map_id
        footprint = dict(width_m=.2, length_m=.2, margin_m=.05)
        preview = self.client.post(base + '/preview', json=footprint)
        self.assertEqual(preview.status_code, 200)
        state = preview.json()
        self.assertGreater(state['driveable_cells'], 0)
        self.assertEqual(state['origin_x_m'], -1)
        self.assertEqual(state['data'][20 * 40 + 14], 0)
        self.assertEqual(state['data'][20 * 40 + 22], 0)
        self.assertIsNone(self.client.get('/api/state').json()['navigation_map'])

        invalid = dict(footprint, start=[0, 0], destinations={'검사장': [22, 20]})
        self.assertEqual(self.client.post(base + '/register', json=invalid).status_code, 422)
        registration = dict(invalid, start=[14, 20], start_position_m=[.44, 2.03], start_heading_deg=270)
        result = self.client.post(base + '/register', json=registration)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['navigation_map']['data'], state['data'])
        self.assertEqual(result.json()['navigation_map']['start_heading_deg'], 270)
        self.assertEqual(result.json()['position'], [.44, 2.03])
        destination = self.client.post('/api/commands', json={'text': '검사장으로 이동해줘'})
        self.assertTrue(destination.json()['accepted'])
        self.assertTrue(self.client.post('/api/stop').json()['accepted'])
        self.assertEqual(set(self.client.get('/api/mapping/state').json()['data']), {-1})

    def test_registration_rejects_disconnected_destinations_without_replacing_map(self):
        grid = OccupancyGrid(config(width_m=2, height_m=1))
        grid.observed[:] = [True] * 200
        grid.log_odds[:] = [-2] * 200
        for row in range(10):
            grid.log_odds[row * 20 + 9] = 2
        mapping_api.grid = grid
        map_id = self.client.post('/api/mapping/save').json()['map_id']
        base = '/api/navigation/sensor-maps/' + map_id
        body = dict(width_m=.06, length_m=.08, margin_m=0,
                    start=[3, 4], destinations={'통로': [6, 4]})
        registered = self.client.post(base + '/register', json=body)
        self.assertEqual(registered.status_code, 200)
        result = self.client.post(base + '/register', json=dict(body, destinations={'반대편': [15, 4]}))
        self.assertEqual(result.status_code, 422)
        self.assertIn('도달할 수 없는 목적지', result.json()['detail'])
        self.assertEqual(self.client.get('/api/state').json()['map_id'], registered.json()['map_id'])
