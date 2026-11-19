import json
import math
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import main
from backend.service import RobotService


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(main, 'service', RobotService())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = self.enterContext(TestClient(main.app))

    def test_dashboard_and_assets(self):
        for path in ['/', '/static/style.css', '/static/app.js', '/static/map-editor.js']:
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn('id="explanation"', self.client.get('/').text)
        self.assertIn('id="command-controls" disabled', self.client.get('/').text)
        for asset in ('mapping.js', 'mapping-demo.js', 'mapping-view.js'):
            response = self.client.get('/static/' + asset)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers['content-type'].startswith('text/javascript'))
        for path in ['/about/', '/about/styles.css', '/about/app.js']:
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn('LANGUAGE INTO MOTION', self.client.get('/about/').text)

    def test_fault_stop_and_recovery_flow(self):
        self.assertEqual(self.client.put('/api/navigation/map', json=self.demo_map()).status_code, 200)
        self.assertTrue(self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()['accepted'])
        state = self.client.post('/api/mock/conditions', json={'obstacle': True, 'localization_valid': True}).json()
        self.assertEqual(state['status'], 'stopped')
        result = self.client.post('/api/commands', json={'text': '충전소로 가줘'}).json()
        self.assertEqual(result['reason'], 'obstacle')
        self.assertTrue(self.client.post('/api/stop').json()['accepted'])
        state = self.client.post('/api/mock/conditions', json={'obstacle': False, 'localization_valid': True}).json()
        self.assertIsNone(state['target'])
        self.assertEqual(state['status'], 'stopped')
        self.assertTrue(self.client.post('/api/commands', json={'text': '검사 구역으로 가줘'}).json()['accepted'])

    def demo_map(self):
        return self.client.get('/static/navigation-demo.json').json()

    def test_initial_pose_updates_position_heading_and_saved_map(self):
        grid = self.client.get('/static/navigation-preliminary.json').json()
        loaded = self.client.put('/api/navigation/map', json=grid).json()
        body = {'map_id': loaded['map_id'], 'cell': [9, 9], 'heading_deg': 0}
        path = '/api/navigation/initial-pose'
        for invalid in (
            {**body, 'map_id': '0' * 32},
            {**body, 'cell': [1, 1]},
            {**body, 'heading_deg': 360},
        ):
            self.assertEqual(self.client.put(path, json=invalid).status_code, 422)
        self.assertEqual(self.client.get('/api/state').json()['position'], loaded['position'])
        applied = self.client.put(path, json=body)
        self.assertEqual(applied.status_code, 200, applied.text)
        state = applied.json()
        for axis in state['position']:
            self.assertAlmostEqual(axis, .475)
        self.assertAlmostEqual(state['heading_rad'], math.pi / 2)
        saved = state['navigation_map']
        self.assertEqual(saved['start'], [9, 9])
        self.assertEqual(saved['start_heading_deg'], 0)
        self.assertEqual(saved['destinations']['시작점'], [9, 9])
        self.assertEqual(saved['destinations']['직진 지점'], grid['destinations']['직진 지점'])
        reloaded = self.client.put('/api/navigation/map', json=saved).json()
        self.assertEqual(reloaded['position'], state['position'])
        self.assertAlmostEqual(reloaded['heading_rad'], math.pi / 2)

        moving = self.client.post('/api/trial/motion', json={
            'map_id': reloaded['map_id'], 'action': 'forward',
        }).json()
        end = moving['route'][-1]
        self.assertAlmostEqual(end[0], reloaded['position'][0])
        self.assertAlmostEqual(end[1], reloaded['position'][1] + .1)
        blocked = self.client.put(path, json={**body, 'map_id': reloaded['map_id'], 'heading_deg': 270})
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(self.client.get('/api/state').json()['status'], 'moving')

    def test_initial_pose_keeps_exact_metric_coordinates_on_reload_and_return(self):
        grid = {**self.demo_map(), 'origin_x_m': -1, 'origin_y_m': .5}
        state = self.client.put('/api/navigation/map', json=grid).json()
        path = '/api/navigation/initial-pose'
        body = {'map_id': state['map_id'], 'position_m': [-.34, 1.18], 'heading_deg': 315}
        for invalid in ({**body, 'position_m': [-.94, 1.18]}, {**body, 'cell': [1, 1]}):
            self.assertEqual(self.client.put(path, json=invalid).status_code, 422)

        applied = self.client.put(path, json=body)
        self.assertEqual(applied.status_code, 200, applied.text)
        state = applied.json()
        self.assertEqual(state['position'], body['position_m'])
        self.assertEqual(state['navigation_map']['start'], [1, 1])
        self.assertEqual(state['navigation_map']['start_position_m'], body['position_m'])
        reloaded = self.client.put('/api/navigation/map', json=state['navigation_map']).json()
        self.assertEqual(reloaded['position'], body['position_m'])

        command = self.client.post('/api/commands', json={'text': '출고대 들렀다가 시작점으로 가줘'}).json()
        self.assertTrue(command['accepted'], command)
        route = [reloaded['position'], *self.client.get('/api/state').json()['route']]
        self.assertEqual(route[-1], body['position_m'])
        self.assertTrue(all(x1 == x2 or y1 == y2 for (x1, y1), (x2, y2) in zip(route, route[1:])))

    def test_preliminary_map_runs_one_manual_motion_at_a_time(self):
        grid = self.client.get('/static/navigation-preliminary.json').json()
        loaded = self.client.put('/api/navigation/map', json=grid).json()
        position = loaded['position']
        url = '/api/trial/motion'
        command = {'map_id': loaded['map_id']}
        response = self.client.post(url, json={**command, 'action': 'left'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.post(url, json={**command, 'action': 'forward'}).status_code, 422)
        with main.service.lock:
            main.service.last_tick -= 0.5
            main.service.tick()
            self.assertEqual(main.service.position, position)
            self.assertAlmostEqual(main.service.heading_rad, math.pi / 2)
            self.assertEqual(main.service.status, 'arrived')

        response = self.client.post(url, json={**command, 'action': 'forward'})
        self.assertEqual(response.status_code, 200, response.text)
        goal = response.json()['route'][-1]
        self.assertAlmostEqual(goal[0], position[0])
        self.assertAlmostEqual(goal[1], position[1] + 0.1)
        self.client.post('/api/stop')
        self.assertEqual(self.client.get('/api/state').json()['route'], [])
        result = self.client.post('/api/commands', json={'text': '직진 지점에 가줘'}).json()
        self.assertEqual(result['reason'], 'trial_controls_required')

    def test_register_destination_on_active_map(self):
        demo = self.demo_map()
        loaded = self.client.put('/api/navigation/map', json=demo).json()
        endpoint = '/api/navigation/destinations'
        place = {'map_id': loaded['map_id'], 'name': '작업대', 'cell': [2, 2]}
        self.assertTrue(self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()['accepted'])
        self.assertEqual(self.client.post(endpoint, json={**place, 'cell': [3, 1]}).status_code, 422)
        response = self.client.post(endpoint, json=place)
        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertEqual(state['target'], '출고대')
        self.assertEqual(state['navigation_map']['destinations']['작업대'], [2, 2])
        self.assertEqual(state['destinations']['작업대'], [1.25, 1.25])
        self.assertEqual(self.client.post(endpoint, json=place).status_code, 422)

        result = self.client.post('/api/commands', json={'text': '작업대에 가줘'}).json()
        self.assertTrue(result['accepted'])
        self.assertEqual(self.client.get('/api/state').json()['route'][-1], [1.25, 1.25])

        result = self.client.post('/api/commands', json={
            'text': '출고대 갔다가 작업대 간 뒤 검사 구역으로 돌아와',
        }).json()
        self.assertTrue(result['accepted'])
        self.assertEqual(result['via'], ['출고대', '작업대'])
        self.assertEqual(result['destination'], '검사 구역')
        route = self.client.get('/api/state').json()['route']
        self.assertIn([1.25, 1.25], route[route.index([3.25, 0.75]) + 1:-1])
        self.assertEqual(route[-1], [3.25, 2.25])

        self.client.put('/api/navigation/map', json=demo)
        self.assertEqual(self.client.post(endpoint, json=place).status_code, 422)
        self.assertNotIn('작업대', self.client.get('/api/state').json()['destinations'])

    def test_move_via_registered_place(self):
        self.assertEqual(self.client.put('/api/navigation/map', json=self.demo_map()).status_code, 200)
        with main.service.lock:
            result = main.service.command('검사 구역을 갔다가 출고대로 이동해줘')
            self.assertTrue(result['accepted'])
            self.assertEqual((result['via'], result['destination']), ('검사 구역', '출고대'))
            inspection = [3.25, 2.25]
            self.assertIn(inspection, main.service.waypoints)
            start_time = main.service.last_tick
            ticks = [start_time + 0.3125 * step for step in range(1, 41)]
            with patch('backend.service.time.monotonic', side_effect=ticks):
                for _ in ticks:
                    main.service.tick()
            self.assertEqual(main.service.position, [3.25, 0.75])
            self.assertEqual(main.service.status, 'arrived')

    def test_unreachable_final_leg_rejects_entire_command(self):
        demo = self.demo_map()
        demo['data'][4 * demo['cols'] + 3] = -1
        self.assertEqual(self.client.put('/api/navigation/map', json=demo).status_code, 200)
        result = self.client.post('/api/commands', json={'text': '충전소에 들렀다가 출고대로 가줘'}).json()
        self.assertEqual(result['reason'], 'no_route')
        state = self.client.get('/api/state').json()
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(state['route'], [])

    def test_zone_registration_routes_to_its_arrival_point_and_survives_reload(self):
        loaded = self.client.put('/api/navigation/map', json=self.demo_map()).json()
        url = '/api/navigation/destinations'
        zone = {'map_id': loaded['map_id'], 'name': '작업 구역',
                'cell': [2, 2], 'zone_cells': [[2, 2], [1, 2]]}
        response = self.client.post(url, json=zone)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()['navigation_map']
        self.assertEqual(saved['zones']['작업 구역'], [[2, 2], [1, 2]])
        self.assertEqual(self.client.post(url, json={**zone, 'name': '겹친 구역'}).status_code, 422)
        self.assertEqual(self.client.put('/api/navigation/map', json=saved).status_code, 200)
        result = self.client.post('/api/commands', json={'text': '작업 구역에 가줘'}).json()
        self.assertTrue(result['accepted'], result)
        self.assertEqual(self.client.get('/api/state').json()['route'][-1], [1.25, 1.25])

    def test_delete_zone_on_active_route_and_reload_empty_places(self):
        demo = self.demo_map()
        demo['destinations'] = {'출고대': [6, 1]}
        demo['zones'] = {'출고대': [[6, 1], [6, 2]]}
        loaded = self.client.put('/api/navigation/map', json=demo)
        self.assertEqual(loaded.status_code, 200, loaded.text)
        map_id = loaded.json()['map_id']
        url = '/api/navigation/destinations/출고대'
        result = self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()
        self.assertTrue(result['accepted'])
        self.assertEqual(self.client.delete(url, params={'map_id': 'old-map'}).status_code, 422)

        removed = self.client.delete(url, params={'map_id': map_id})
        self.assertEqual(removed.status_code, 200, removed.text)
        state = removed.json()
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['route'], [])
        self.assertEqual(state['destinations'], {})
        self.assertEqual(state['navigation_map']['zones'], {})
        self.assertEqual(self.client.put('/api/navigation/map', json=state['navigation_map']).status_code, 200)
        result = self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()
        self.assertEqual(result['reason'], 'unknown_destination')

    def test_colloquial_return_via_inspection_and_start(self):
        maze = self.client.get('/static/navigation-maze.json').json()
        for text, registered_start in (
            ('검사 구역 들렀다가 시작 간 뒤 충전소로 와줘', True),
            ('검사 구역 들렸다가 시작 점 간 뒤 충전소로 와줘', True),
            ('검사 구역 들렸다가 시작 점 간 뒤 충전소로 와줘', False),
        ):
            with self.subTest(text=text, registered_start=registered_start):
                destinations = maze['destinations'].copy()
                if not registered_start:
                    destinations.pop('시작')
                state = self.client.put('/api/navigation/map', json={**maze, 'destinations': destinations})
                self.assertEqual(state.status_code, 200)
                places = state.json()['destinations']
                start_position = state.json()['position']
                result = self.client.post('/api/commands', json={'text': text}).json()
                self.assertTrue(result['accepted'], result)
                self.assertEqual(result['via'], ['검사 구역', '시작' if registered_start else '시작점'])
                self.assertEqual(result['destination'], '충전소')
                route = self.client.get('/api/state').json()['route']
                self.assertLess(route.index(places['검사 구역']), route.index(start_position))
                self.assertEqual(route[-1], places['충전소'])

    def test_visit_and_wait_using_colloquial_delay(self):
        maze = self.client.get('/static/navigation-maze.json').json()
        state = self.client.put('/api/navigation/map', json=maze)
        self.assertEqual(state.status_code, 200)
        for command, via, destination in (
            ('검사 구역 다녀왔다가 1초 뒤에 충전소로 이동해줘', '검사 구역', '충전소'),
            ('충전소 가서 1초 대기 후 시작으로 이동', '충전소', '시작'),
        ):
            with self.subTest(command=command):
                result = self.client.post('/api/commands', json={'text': command}).json()
                self.assertTrue(result['accepted'], result)
                self.assertEqual((result['via'], result['wait_seconds'], result['destination']),
                                 (via, 1, destination))

    def test_operator_phrasing_on_the_demo_map(self):
        maze = self.client.get('/static/navigation-maze.json').json()
        self.assertEqual(self.client.put('/api/navigation/map', json=maze).status_code, 200)
        for text, destination, via, seconds in (
            ('로봇아, 지금 충전소로 가줘', '충전소', None, None),
            ('자재 보관소로 좀 가줘', '자재 보관소', None, None),
            ('출고대로 이동 부탁해요', '출고대', None, None),
            ('목적지를 충전소로 바꿔줘', '충전소', None, None),
            ('검사 구역 먼저 갔다 오고 출고대로 가자', '출고대', '검사 구역', None),
            ('검사 구역, 출고대, 충전소 순서대로 방문해줘', '충전소', ['검사 구역', '출고대'], None),
            ('검사 구역 가서 1분 기다렸다가 충전소로 가줘', '충전소', '검사 구역', 60),
            ('검사 구역에서 두 초 기다렸다가 출고대로 가줘', '출고대', '검사 구역', 2),
            ('충전소 가서 5초만 쉬고 시작으로 다시 와줘', '시작', '충전소', 5),
        ):
            with self.subTest(text=text):
                result = self.client.post('/api/commands', json={'text': text}).json()
                self.assertTrue(result['accepted'], result)
                self.assertEqual(
                    (result['intent'], result['destination'], result.get('via'), result.get('wait_seconds')),
                    ('move', destination, via, seconds),
                )

        for text, intent, expected in (
            ('갈 수 있는 구역 알려줘', 'places', '검사 구역'),
            ('충전소까지 몇 초 걸려?', 'estimate', '충전소까지'),
            ('충전소 가는 데 얼마나 걸려?', 'estimate', '충전소까지'),
            ('격리 구역까지 갈 수 있어?', 'estimate', '통행 가능한 경로가 없습니다'),
            ('지금 어디로 가는 중이야?', 'progress', '시작'),
            ('아직 움직이는 중이야?', 'status', '이동 중'),
        ):
            with self.subTest(text=text):
                result = self.client.post('/api/commands', json={'text': text}).json()
                self.assertEqual(result['intent'], intent)
                self.assertIn(expected, result['message'])
                self.assertEqual(self.client.get('/api/state').json()['target'], '시작')

        for text, reason in (
            ('지금은 충전소로 가지 마', 'unknown_intent'),
            ('정지 버튼 누르면 멈춰', 'unknown_intent'),
            ('충전소에서 멈춰', 'unknown_intent'),
            ('검사 구역이나 충전소로 가줘', 'unknown_destination'),
            ('검사 구역에서 6분 기다렸다가 충전소로 가줘', 'invalid_wait'),
        ):
            with self.subTest(text=text):
                result = self.client.post('/api/commands', json={'text': text}).json()
                self.assertFalse(result['accepted'], result)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(self.client.get('/api/state').json()['target'], '시작')

        for text in ('지금 바로 멈춰 주세요', '주행 취소해 줘', '여기서 일단 멈추세요'):
            with self.subTest(text=text):
                self.assertTrue(self.client.post('/api/commands', json={'text': '충전소로 가줘'}).json()['accepted'])
                result = self.client.post('/api/commands', json={'text': text}).json()
                self.assertTrue(result['accepted'], result)
                self.assertEqual(result['intent'], 'stop')
                self.assertEqual(self.client.get('/api/state').json()['route'], [])

    def test_ordered_places_resolve_spacing_and_keep_visit_order(self):
        self.assertEqual(self.client.put('/api/navigation/map', json=self.demo_map()).status_code, 200)
        for text in ('검사구역, 출고대, 충전소 순서대로 이동해줘',
                     '검사구역, 출고대, 충전소를 순서대로 방문 해줘',
                     '검사 구역, 출고대, 충전소 순으로 방문 해줘'):
            with self.subTest(text=text):
                result = self.client.post('/api/commands', json={'text': text}).json()
                self.assertTrue(result['accepted'])
                self.assertEqual(result['via'], ['검사 구역', '출고대'])
                self.assertEqual(result['destination'], '충전소')
        route = self.client.get('/api/state').json()['route']
        self.assertLess(route.index([3.25, 2.25]), route.index([3.25, 0.75]))
        self.assertEqual(route[-1], [0.75, 0.75])

        rejected = self.client.post('/api/commands', json={
            'text': '검사구역, 미등록장소, 충전소 순서대로 이동해줘',
        }).json()
        self.assertEqual(rejected['reason'], 'unknown_destination')

    def test_map_required_and_replacement_stops_active_route(self):
        result = self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason'], 'map_required')
        initial = self.client.get('/api/state').json()
        self.assertIsNone(initial['position'])
        self.assertEqual(initial['route'], [])

        demo = self.demo_map()
        self.assertEqual(self.client.put('/api/navigation/map', json=demo).status_code, 200)
        self.assertTrue(self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()['accepted'])
        replacement = {**demo, 'start': [2, 2]}
        state = self.client.put('/api/navigation/map', json=replacement).json()
        self.assertEqual(state['status'], 'idle')
        self.assertIsNone(state['target'])
        self.assertEqual(state['route'], [])
        self.assertEqual(state['position'], [1.25, 1.25])

    def test_conversational_navigation_commands_and_queries(self):
        self.assertEqual(self.client.put('/api/navigation/map', json=self.demo_map()).status_code, 200)
        cases = (
            ('로봇아, 출고대까지 가 줄래?', 'move', '출고대', None),
            ('검사 구역 먼저 들르고 출고대에 가 주세요', 'move', '출고대', '검사 구역'),
            ('검사구역이랑 출고대 그리고 충전소 차례대로 방문해 줘', 'move', '충전소', ['검사 구역', '출고대']),
            ('충전하러 가줘', 'move', '충전소', None),
        )
        for text, intent, destination, via in cases:
            with self.subTest(text=text):
                result = self.client.post('/api/commands', json={'text': text}).json()
                self.assertTrue(result['accepted'])
                self.assertEqual((result['intent'], result['destination'], result.get('via')), (intent, destination, via))

        self.assertEqual(self.client.put('/api/navigation/map', json=self.demo_map()).status_code, 200)
        self.client.post('/api/stop')
        for text, intent, expected in (
            ('어디로 갈 수 있어?', 'places', '검사 구역'),
            ('출고대까지 얼마나 걸려?', 'estimate', '5.5 m'),
            ('지금 어디쯤이야?', 'position', '모의 위치'),
            ('왜 멈춰 있어?', 'status', '정지'),
        ):
            result = self.client.post('/api/commands', json={'text': text}).json()
            self.assertEqual(result['intent'], intent)
            self.assertIn(expected, result['message'])
            self.assertIsNone(self.client.get('/api/state').json()['target'])

        self.assertTrue(self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()['accepted'])
        progress = self.client.post('/api/commands', json={'text': '얼마나 남았어?'}).json()
        self.assertIn('출고대', progress['message'])
        self.assertIn('모의 주행 속도', progress['message'])
        stop = self.client.post('/api/commands', json={'text': '그만 가!'}).json()
        self.assertEqual(stop['intent'], 'stop')
        self.assertIsNone(self.client.get('/api/state').json()['target'])

    def test_invalid_requests(self):
        for body in [{}, {'text': ''}, {'text': 'x' * 201}]:
            self.assertEqual(self.client.post('/api/commands', json=body).status_code, 422)
        for body in [{}, {'obstacle': 'false', 'localization_valid': True}]:
            self.assertEqual(self.client.post('/api/mock/conditions', json=body).status_code, 422)

    def test_imported_map_route_and_arrival(self):
        demo = json.loads((main.frontend / 'navigation-demo.json').read_text(encoding='utf-8'))
        demo.update(origin_x_m=-2, origin_y_m=1)
        response = self.client.put('/api/navigation/map', json=demo)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['position'], [-1.25, 1.75])

        result = self.client.post('/api/commands', json={'text': '출고대로 가줘'}).json()
        self.assertTrue(result['accepted'])
        with main.service.lock:
            state = main.service.snapshot()
            self.assertEqual(state['planner'], 'grid_astar')
            route = [state['position'], *state['route']]
            crossed = [y1 + (y2 - y1) * (-0.25 - x1) / (x2 - x1)
                       for (x1, y1), (x2, y2) in zip(route, route[1:])
                       if x1 < -0.25 <= x2]
            self.assertEqual(crossed, [3.25])
            for _ in range(25):
                main.service.last_tick -= 0.5
                main.service.tick()
            self.assertEqual(main.service.status, 'arrived')
            self.assertEqual(main.service.position, [1.25, 1.75])

        self.assertTrue(self.client.post('/api/commands', json={'text': '검사 구역으로 이동해 주세요'}).json()['accepted'])
        self.client.post('/api/stop')
        state = self.client.get('/api/state').json()
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['route'], [])

    def test_unreachable_and_invalid_map_preserve_state(self):
        demo = json.loads((main.frontend / 'navigation-demo.json').read_text(encoding='utf-8'))
        demo['data'][4 * demo['cols'] + 3] = -1
        self.assertEqual(self.client.put('/api/navigation/map', json=demo).status_code, 200)
        for text, reason in [('출고대로 가줘', 'no_route'), ('미등록 장소로 가줘', 'unknown_destination'),
                             ('출고대와 충전소로 가줘', 'unknown_destination'),
                             ('출고대로 가지 마', 'unknown_intent')]:
            result = self.client.post('/api/commands', json={'text': text}).json()
            self.assertEqual(result['reason'], reason)
        invalid = {**demo, 'start': [3, 0]}
        self.assertEqual(self.client.put('/api/navigation/map', json=invalid).status_code, 422)
        state = self.client.get('/api/state').json()
        self.assertEqual(state['navigation_map']['start'], demo['start'])
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(state['route'], [])


if __name__ == '__main__':
    unittest.main()
