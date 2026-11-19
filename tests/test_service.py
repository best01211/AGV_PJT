import math
import unittest
from unittest.mock import patch
from backend.navigation import NavigationMap
from backend.service import RobotService, parse_command


def robot_with_map():
    robot = RobotService()
    robot.load_navigation_map(NavigationMap(
        schema_version=1, frame_id='test', resolution_m=0.5,
        origin_x_m=-0.25, origin_y_m=-0.25, cols=16, rows=12, data=[0] * 192,
        start=(2, 2), destinations={'A': (4, 4), 'B': (14, 4), 'C': (14, 10)},
    ))
    return robot


class CommandsTest(unittest.TestCase):
    def test_destinations(self):
        for text, destination in [('A 지점으로 이동해 줘','A'),('B로 가줘','B'),('C 지점으로 이동','C')]:
            robot = robot_with_map()
            self.assertTrue(robot.command(text)['accepted'])
            self.assertEqual(robot.snapshot()['target'], destination)

    def test_stop_preempts_move(self):
        robot = robot_with_map()
        robot.command('A로 가줘')
        robot.command('정지해')
        position = robot.snapshot()['position']
        robot.last_tick -= 1
        robot.tick()
        self.assertEqual(robot.snapshot()['position'], position)
        self.assertIsNone(robot.target)
        self.assertEqual(parse_command('A로 이동하다 멈춰')['intent'], 'stop')

    def test_unknown_and_ambiguous_do_not_move(self):
        for text in ['A와 B로 이동', 'ABC로 이동', '안녕하세요', '']:
            robot = robot_with_map()
            self.assertFalse(robot.command(text)['accepted'])
            self.assertIsNone(robot.target)

    def test_position_and_arrival(self):
        robot = robot_with_map()
        self.assertEqual(robot.command('현재 위치 알려줘')['intent'], 'position')
        robot.command('A로 가줘')
        route = [robot.position, *robot.snapshot()['route']]
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1] for a, b in zip(route, route[1:])))
        with patch('backend.service.time.monotonic', return_value=robot.last_tick + 0.25):
            robot.tick()
        self.assertEqual(robot.position, [1.0, 1.0])
        self.assertAlmostEqual(robot.snapshot()['heading_rad'], math.pi / 4)
        with patch('backend.service.time.monotonic', return_value=robot.last_tick + 0.375):
            robot.tick()
        self.assertEqual(robot.position, [1.0, 1.1])
        self.assertAlmostEqual(robot.snapshot()['heading_rad'], math.pi / 2)

        self.assertTrue(robot.command('A로 가줘')['accepted'])
        self.assertEqual(robot.snapshot()['route'][0], [1.0, 1.5])
        for _ in range(16):
            robot.last_tick -= 0.5
            robot.tick()
        self.assertEqual(robot.status, 'arrived')
        self.assertEqual(robot.position, [2.0, 2.0])

    def test_diagonal_cannot_cut_a_blocked_corner(self):
        grid = NavigationMap(
            schema_version=1, frame_id='corner', resolution_m=1,
            cols=2, rows=2, data=[0, 100, -1, 0],
            start=(0, 0), destinations={'A': (1, 1)},
        )
        self.assertIsNone(grid.any_angle_route(grid.center(grid.start), (1, 1)))

    def test_route_accounts_for_footprint_in_a_passage(self):
        for opening, can_move in (((3, 4), False), ((2, 3, 4, 5), True)):
            with self.subTest(opening=opening):
                grid = NavigationMap(
                    schema_version=1, frame_id='passage', resolution_m=.1,
                    cols=11, rows=9,
                    data=[100 if col == 5 and row not in opening else 0
                          for row in range(9) for col in range(11)],
                    start=(2, 3), destinations={'A': (8, 3)},
                )
                robot = RobotService()
                state = robot.load_navigation_map(grid)
                self.assertIn(0, state['footprint_blocked_cells'])
                self.assertEqual(robot.command('A로 가줘')['accepted'], can_move)

    def test_preliminary_forward_stops_before_body_reaches_boundary(self):
        grid = NavigationMap(
            schema_version=1, frame_id='preliminary-1m', resolution_m=.05,
            cols=20, rows=20, data=[0] * 400, start=(16, 10), destinations={},
        )
        robot = RobotService()
        state = robot.load_navigation_map(grid)
        with self.assertRaisesRegex(ValueError, '통행할 수 없습니다'):
            robot.start_trial_motion(state['map_id'], 'forward')
        self.assertEqual(robot.position, grid.center(grid.start))

    def test_timed_visit_waits_at_start_and_resumes_or_stops(self):
        for stop_during_wait in (False, True):
            with self.subTest(stop_during_wait=stop_during_wait):
                robot = robot_with_map()
                start = robot.position.copy()
                robot.position = robot.destinations['A'].copy()
                result = robot.command('시작점에서 1초간 대기 후 B로 다시 와줘')
                self.assertTrue(result['accepted'], result)
                self.assertEqual(result['wait_seconds'], 1)

                def tick():
                    with patch('backend.service.time.monotonic', return_value=robot.last_tick + 0.25):
                        robot.tick()

                for _ in range(20):
                    tick()
                    if robot.status == 'waiting':
                        break
                self.assertEqual((robot.status, robot.position), ('waiting', start))
                for _ in range(3):
                    tick()
                self.assertEqual((robot.status, robot.position), ('waiting', start))
                if stop_during_wait:
                    robot.command('멈춰')
                for _ in range(52):
                    tick()
                expected = ('stopped', start) if stop_during_wait else ('arrived', robot.destinations['B'])
                self.assertEqual((robot.status, robot.position), expected)

    def test_untrusted_intent_validation(self):
        robot = robot_with_map()
        for parsed in [None, {}, {'intent': 'move', 'destination': 'D'},
                       {'intent': 'move', 'destination': ['A']},
                       {'intent': 'motor', 'destination': None},
                       {'intent': 'move', 'destination': 'A', 'speed': 99},
                       {'intent': 'stop', 'destination': 'A'}]:
            self.assertFalse(robot.execute_intent(parsed)['accepted'])
            self.assertIsNone(robot.target)
        self.assertEqual(robot.command('D로 가줘')['reason'], 'unknown_destination')

    def test_fault_stops_and_requires_explicit_restart(self):
        for obstacle, valid in [(True, True), (False, False)]:
            robot = robot_with_map()
            robot.command('B로 가줘')
            position = robot.position.copy()
            robot.set_mock_conditions(obstacle, valid)
            self.assertEqual(robot.status, 'stopped')
            self.assertFalse(robot.command('C로 가줘')['accepted'])
            robot.last_tick -= 0.5
            robot.tick()
            self.assertEqual(robot.position, position)
            self.assertTrue(robot.command('정지해')['accepted'])
            robot.set_mock_conditions(False, True)
            self.assertIsNone(robot.target)
            self.assertEqual(robot.status, 'stopped')
            self.assertTrue(robot.command('C로 가줘')['accepted'])

    def test_explanation_uses_stop_event_after_rejected_command(self):
        robot = robot_with_map()
        robot.command('A로 가줘')
        robot.set_mock_conditions(True, True)
        stopped = robot.snapshot()['explanation']
        robot.command('B로 가줘')
        self.assertEqual(robot.snapshot()['explanation'], stopped)
        self.assertIn('장애물', robot.command('왜 멈췄어?')['message'])
        events = robot.snapshot()['events']
        evidence = next(e for e in events if e['id'] == stopped['evidence_event_id'])
        self.assertEqual(evidence['reason'], 'obstacle')
        self.assertEqual(evidence['previous_target'], 'A')

    def test_snapshots_cannot_mutate_service(self):
        robot = robot_with_map()
        state = robot.snapshot()
        state['destinations']['A'][0] = 99
        state['conditions']['obstacle'] = True
        self.assertEqual(robot.destinations['A'], [2.0, 2.0])
        self.assertFalse(robot.conditions['obstacle'])

    def test_arrival_event_and_lost_position(self):
        robot = robot_with_map()
        robot.command('A로 가줘')
        for _ in range(10):
            robot.last_tick -= 0.5
            robot.tick()
        self.assertEqual(robot.events[0]['type'], 'arrived')
        self.assertEqual(robot.events[0]['destination'], 'A')
        robot.set_mock_conditions(False, False)
        self.assertIn('마지막 좌표', robot.command('현재 위치 알려줘')['message'])


if __name__ == '__main__':
    unittest.main()
