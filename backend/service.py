"""하드웨어 I/O가 없는 명령 및 주행 시뮬레이터."""
import math
from copy import deepcopy
import threading
import time
from collections import deque
from datetime import datetime, timezone
from uuid import uuid4
from ai.command_parser import MAX_WAIT_SECONDS, START_POINT, normalize_name, parse_command, validate_intent
from backend.agv_geometry import heading_to_yaw


MOCK_SPEED_MPS = 0.8
MOCK_TURN_RAD_PER_S = math.pi
TRIAL_FRAME_ID = 'preliminary-1m'
TRIAL_FORWARD_M = 0.10
TRIAL_SPEED_MPS = 0.10
TRIAL_ACTION_LABELS = {'forward': '10cm 직진', 'left': '왼쪽 90도 회전', 'right': '오른쪽 90도 회전'}


REASONS = {
    'invalid_structure': '명령 형식을 확인해 주세요. 순서대로 이동할 장소는 쉼표로 구분해 최대 10곳까지 입력할 수 있습니다.',
    'invalid_wait': f'대기 시간은 1~{MAX_WAIT_SECONDS}초 사이의 정수로 입력해 주세요.',
    'unknown_intent': '명령을 확인해 주세요. 예: A로 가줘 / 정지해 / 현재 상태 알려줘',
    'unknown_destination': '등록되지 않은 목적지입니다. 지도에 표시된 이름을 사용해 주세요.',
    'localization_lost': '모의 위치 추정을 사용할 수 없습니다.',
    'obstacle': '모의 장애물 조건이 활성화되어 있습니다.',
    'no_route': '현재 위치에서 목적지까지 통행 가능한 경로가 없습니다.',
    'map_required': '주행 지도를 먼저 만들거나 불러와 주세요.',
    'trial_controls_required': '예비 주행에서는 직진·회전 버튼을 사용해 주세요.',
}


class RobotService:
    def __init__(self):
        self.lock = threading.RLock()
        self.position = None
        self.heading_rad = 0.0
        self.trial_heading = None
        self.destinations = {}
        self.target = None
        self.active_destinations = set()
        self.navigation_map = None
        self.map_id = None
        self.waypoints = deque()
        self.wait_waypoint = None
        self.wait_remaining_s = 0
        self.status = 'idle'
        self.history = deque(maxlen=50)
        self.events = deque(maxlen=100)
        self.event_sequence = 0
        self.conditions = {'obstacle': False, 'localization_valid': True}
        self.last_tick = time.monotonic()
        self._event('initialized', REASONS['map_required'])

    def load_navigation_map(self, navigation_map):
        with self.lock:
            if not navigation_map.can_start_at(navigation_map.start, navigation_map.start_position):
                raise ValueError('시작 좌표에서 20 × 20cm 로봇이 회전하거나 주행 경로에 연결할 공간이 부족합니다.')
            self.navigation_map = navigation_map
            self.map_id = uuid4().hex
            self.destinations = {name: navigation_map.destination_position(name) for name in navigation_map.destinations}
            self._place_at_start()
            self._event('map_loaded', '주행 지도를 불러왔습니다. 지도 시작점에서 대기 중입니다.')
            return self.snapshot()

    def _place_at_start(self):
        grid = self.navigation_map
        self.position = grid.start_position
        self.heading_rad = heading_to_yaw(grid.start_heading_deg)
        self.trial_heading = None
        self.waypoints.clear()
        self.wait_waypoint = None
        self.wait_remaining_s = 0
        self.target = None
        self.active_destinations.clear()
        self.status = 'idle'

    def set_initial_pose(self, map_id, cell, heading_deg, *, position_m=None):
        with self.lock:
            grid = self.navigation_map
            if grid is None or map_id != self.map_id:
                raise ValueError('지도가 변경되었습니다. 현재 지도에서 초기 위치를 다시 선택해 주세요.')
            if self.status in ('moving', 'waiting'):
                raise ValueError('주행을 정지하거나 완료한 뒤 초기 위치와 방향을 지정하세요.')
            if position_m is not None:
                cell = grid.cell_at(position_m)
            position = position_m if position_m is not None else grid.center(cell) if grid.is_driveable(cell) else None
            if position is None or not grid.can_start_at(cell, position):
                raise ValueError('지도 안에서 20 × 20cm 로봇이 회전하고 출발할 공간이 있는 좌표를 선택하세요.')
            start_names = [name for name in grid.destinations if grid.is_start_destination(name)]
            grid.start = cell
            grid.start_position_m = position_m
            grid.start_heading_deg = heading_deg
            for name in start_names:
                grid.destinations[name] = cell
                self.destinations[name] = grid.start_position
            self._place_at_start()
            x, y = self.position
            self._event('initial_pose_set', f'초기 위치 X {x:g} m, Y {y:g} m, 방향 {heading_deg}°를 지정했습니다.')
            return self.snapshot()

    def add_destination(self, map_id, name, cell, zone_cells=None):
        with self.lock:
            grid = self.navigation_map
            if grid is None or map_id != self.map_id:
                raise ValueError('지도가 변경되었습니다. 현재 지도에서 위치를 다시 선택해 주세요.')
            if not grid.is_driveable(cell):
                raise ValueError('20 × 20cm 로봇이 통행할 수 있는 칸에만 목적지를 등록할 수 있습니다.')
            if zone_cells is not None:
                if any(not grid.is_driveable(zone_cell) for zone_cell in zone_cells):
                    raise ValueError('구역에는 20 × 20cm 로봇이 통행할 수 있는 칸만 포함하세요.')
                existing = next((place for place in self.destinations
                                 if normalize_name(place) == normalize_name(name)), None)
                if existing in grid.zones:
                    raise ValueError('이미 등록된 구역 이름입니다.')
                name = existing or name
                cell = grid.destinations.get(name, cell)
                updated = type(grid).model_validate({
                    **grid.model_dump(),
                    'destinations': {**grid.destinations, name: cell},
                    'zones': {**grid.zones, name: zone_cells},
                })
                self.navigation_map = updated
                self.destinations[name] = updated.destination_position(name)
                return self.snapshot()
            if any(normalize_name(existing) == normalize_name(name) for existing in self.destinations):
                raise ValueError('이미 등록된 목적지 이름입니다.')
            if len(self.destinations) >= 50:
                raise ValueError('목적지는 지도당 최대 50곳까지 등록할 수 있습니다.')
            grid.destinations[name] = cell
            self.destinations[name] = grid.destination_position(name)
            return self.snapshot()

    def remove_destination(self, map_id, name):
        with self.lock:
            grid = self.navigation_map
            if grid is None or map_id != self.map_id:
                raise ValueError('지도가 변경되었습니다. 현재 지도에서 장소를 다시 선택해 주세요.')
            if name not in grid.destinations:
                raise ValueError('이미 삭제됐거나 등록되지 않은 장소입니다.')
            del grid.destinations[name]
            grid.zones.pop(name, None)
            del self.destinations[name]
            if name in self.active_destinations:
                self._halt('destination_removed', f'{name}이(가) 삭제되어 주행을 정지했습니다.')
            else:
                self._event('destination_removed', f'{name}이(가) 지도에서 삭제됐습니다.')
            return self.snapshot()

    @property
    def planner(self):
        return 'grid_astar' if self.navigation_map else None

    def _event(self, kind, message, **details):
        self.event_sequence += 1
        event = {'id': self.event_sequence, 'type': kind, 'message': message,
                 'timestamp': datetime.now(timezone.utc).isoformat(),
                 'position': self.position.copy() if self.position is not None else None, 'target': self.target,
                 'conditions': self.conditions.copy(), **details}
        self.events.appendleft(event)
        return event

    def _blocked_reason(self):
        if not self.conditions['localization_valid']:
            return 'localization_lost'
        if self.conditions['obstacle']:
            return 'obstacle'
        return None

    def _halt(self, reason, message=None):
        previous_target = self.target
        self.target = None
        self.trial_heading = None
        self.active_destinations.clear()
        self.waypoints.clear()
        self.wait_waypoint = None
        self.wait_remaining_s = 0
        self.status = 'stopped' if self.navigation_map else 'idle'
        return self._event('stopped', message or REASONS.get(reason, '사용자 명령으로 모의 주행을 정지했습니다.'),
                           reason=reason, previous_target=previous_target)

    def start_trial_motion(self, map_id, action):
        with self.lock:
            grid = self.navigation_map
            if grid is None or grid.frame_id != TRIAL_FRAME_ID or map_id != self.map_id:
                raise ValueError('현재 화면에서 1m × 1m 예비 주행 지도를 다시 확인해 주세요.')
            if self.target:
                raise ValueError('동작이 끝나거나 정지한 뒤 다시 시도해 주세요.')
            reason = self._blocked_reason()
            if reason:
                raise ValueError(REASONS[reason])
            label = TRIAL_ACTION_LABELS[action]
            if action == 'forward':
                x, y = self.position
                goal = [x + TRIAL_FORWARD_M * math.cos(self.heading_rad),
                        y + TRIAL_FORWARD_M * math.sin(self.heading_rad)]
                start_grid, end_grid = grid._grid_point(self.position), grid._grid_point(goal)
                if not grid._line_clear(start_grid, end_grid) or not grid.footprint_clear(start_grid, end_grid):
                    raise ValueError('앞쪽 10cm를 통행할 수 없습니다.')
                self.waypoints.append(goal)
            else:
                center = grid._grid_point(self.position)
                if not grid.footprint_clear(center, center):
                    raise ValueError('현재 위치에서 20 × 20cm 로봇이 회전할 공간이 부족합니다.')
                angle = math.pi / 2 if action == 'left' else -math.pi / 2
                self.trial_heading = math.remainder(self.heading_rad + angle, math.tau)
            self.target = label
            self.status = 'moving'
            self.last_tick = time.monotonic()
            self._event('move_started', f'{label} 모의 동작을 시작합니다.')
            return self.snapshot()

    def set_mock_conditions(self, obstacle, localization_valid):
        with self.lock:
            updated = {'obstacle': obstacle, 'localization_valid': localization_valid}
            if updated != self.conditions:
                self.conditions = updated
                self._event('conditions_changed', '모의 시험 조건을 변경했습니다. 해제 후에도 자동 재출발하지 않습니다.')
                reason = self._blocked_reason()
                if reason:
                    self._halt(reason)
            return self.snapshot()

    def tick(self):
        with self.lock:
            now = time.monotonic()
            dt = min(now - self.last_tick, 0.5)
            self.last_tick = now
            if self.target:
                reason = self._blocked_reason()
                if reason:
                    self._halt(reason)
                    return
                trial = self.navigation_map.frame_id == TRIAL_FRAME_ID
                speed = TRIAL_SPEED_MPS if trial else MOCK_SPEED_MPS
                if self.trial_heading is not None:
                    turn = math.remainder(self.trial_heading - self.heading_rad, math.tau)
                    if abs(turn) > dt * MOCK_TURN_RAD_PER_S:
                        self.heading_rad += math.copysign(dt * MOCK_TURN_RAD_PER_S, turn)
                        return
                    self.heading_rad = self.trial_heading
                    self.trial_heading = None
                if self.status == 'waiting':
                    remaining = self.wait_remaining_s - dt
                    self.wait_remaining_s = max(0, remaining)
                    if remaining > 0:
                        return
                    dt = -remaining
                    self.status = 'moving'
                    self._event('wait_ended', f'대기를 마치고 {self.target} 지점으로 이동합니다.')
                while self.waypoints:
                    goal = self.waypoints[0]
                    dx, dy = goal[0] - self.position[0], goal[1] - self.position[1]
                    distance = math.hypot(dx, dy)
                    if distance > 1e-9:
                        heading = math.atan2(dy, dx)
                        turn = math.remainder(heading - self.heading_rad, math.tau)
                        turn_time = abs(turn) / MOCK_TURN_RAD_PER_S
                        if turn_time > dt:
                            self.heading_rad += math.copysign(dt * MOCK_TURN_RAD_PER_S, turn)
                            return
                        self.heading_rad = heading
                        dt -= turn_time
                        travel_time = distance / speed
                        if travel_time > dt:
                            fraction = dt / travel_time
                            self.position[0] += dx * fraction
                            self.position[1] += dy * fraction
                            return
                        dt -= travel_time
                    self.position = self.waypoints.popleft().copy()
                    if goal is self.wait_waypoint:
                        self.wait_waypoint = None
                        self.status = 'waiting'
                        self._event('wait_started', f'경유지에 도착해 {self.wait_remaining_s}초 동안 대기합니다.')
                        return
                destination = self.target
                self.target = None
                self.active_destinations.clear()
                self.status = 'arrived'
                completion = f'{destination} 동작을 완료했습니다.' if trial else '목적지의 모의 좌표에 도착했습니다.'
                self._event('arrived', completion, destination=destination)

    def command(self, text):
        with self.lock:
            return self.execute_intent(parse_command(text, self.destinations), text)

    def _route(self, destination, via=None):
        grid = self.navigation_map
        position = self.position
        stops = [via] if isinstance(via, str) else (via or [])
        route = []
        for name in [*stops, destination]:
            goal = grid.start if name == START_POINT and name not in grid.destinations else grid.destinations[name]
            goal_position = grid.destination_position(name)
            if position == goal_position:
                route.append(goal_position)
                continue
            start_cell = grid.cell_at(position)
            cells = grid.route(start_cell, goal) if start_cell is not None else None
            if not cells:
                return []
            leg = [grid.center(cell) for cell in cells]
            if len(leg) > 1 and (position[0] == leg[1][0] or position[1] == leg[1][1]):
                leg.pop(0)
            entry = grid.connect_positions(position, leg[0])
            exit_path = [] if leg[-1] == goal_position else grid.connect_positions(leg[-1], goal_position)
            if entry is None or exit_path is None:
                return []
            route.extend([*entry, *leg[1:], *exit_path])
            position = goal_position
        return route

    def execute_intent(self, parsed, text=''):
        """파서/향후 LLM의 결과는 반드시 이 검증 경계를 거칩니다."""
        with self.lock:
            reason = validate_intent(parsed, self.destinations)
            intent = parsed.get('intent') if isinstance(parsed, dict) else 'unknown'
            if not self.navigation_map and intent in ('move', 'position', 'places', 'progress', 'estimate') and reason in (None, 'unknown_destination'):
                reason = 'map_required'
            if not reason and intent == 'move':
                reason = self._blocked_reason()
                if not reason and self.navigation_map.frame_id == TRIAL_FRAME_ID:
                    reason = 'trial_controls_required'
                if not reason:
                    route = self._route(parsed['destination'], parsed.get('via'))
                    if not route:
                        reason = 'no_route'
            event = None
            if reason:
                message = REASONS[reason]
                event = self._event('command_rejected', message, reason=reason)
            elif intent == 'move':
                self.target = parsed['destination']
                self.waypoints = deque(route)
                self.status = 'moving'
                via = parsed.get('via')
                stops = [via] if isinstance(via, str) else (via or [])
                self.active_destinations = {*stops, self.target}
                self.wait_remaining_s = parsed.get('wait_seconds', 0)
                self.wait_waypoint = None
                if self.wait_remaining_s:
                    wait_position = self.navigation_map.destination_position(via)
                    self.wait_waypoint = route[route.index(wait_position)]
                    message = f'{via}에서 {self.wait_remaining_s}초 대기한 뒤 {self.target} 지점으로 이동합니다.'
                elif via:
                    message = f'{" → ".join([*stops, self.target])} 순서로 모의 이동을 시작합니다.'
                else:
                    message = f'{self.target} 지점으로 모의 이동을 시작합니다.'
                event = self._event('move_started', message, planner=self.planner, via=via)
            elif intent == 'stop':
                event = self._halt('user_stop')
                message = event['message']
            elif intent == 'position':
                message = f'모의 위치: ({self.position[0]:.2f}, {self.position[1]:.2f}) m'
                if not self.conditions['localization_valid']:
                    message = '모의 위치 추정 실패 상태입니다. 마지막 좌표를 현재 위치로 사용할 수 없습니다.'
            elif intent == 'status':
                message = self.explanation()['message']
            elif intent == 'places':
                message = '등록된 목적지: ' + ', '.join(self.destinations)
            elif intent == 'progress':
                if self.target:
                    message = f'최종 목적지: {self.target}. ' + self._travel_estimate(list(self.waypoints), self.wait_remaining_s)
                else:
                    message = self.explanation()['message']
            elif intent == 'estimate':
                if not self.conditions['localization_valid']:
                    message = REASONS['localization_lost']
                else:
                    destination = parsed['destination']
                    route = self._route(destination)
                    message = f'{destination}까지 {self._travel_estimate(route)}' if route else REASONS['no_route']
            result = dict(intent=intent, destination=parsed.get('destination') if isinstance(parsed, dict) else None,
                          accepted=reason is None, reason=reason, message=message,
                          parser='rules', evidence_event_id=event['id'] if event else self.events[0]['id'],
                          text=text, timestamp=datetime.now(timezone.utc).isoformat())
            if not reason and intent == 'move' and via:
                result['via'] = via
                if self.wait_remaining_s:
                    result['wait_seconds'] = self.wait_remaining_s
            self.history.appendleft(result)
            return deepcopy(result)

    def _travel_estimate(self, route, wait_seconds=0):
        distance = turn = 0.0
        heading = self.heading_rad
        for start, end in zip([self.position, *route], route):
            segment = math.dist(start, end)
            if segment > 1e-9:
                next_heading = math.atan2(end[1] - start[1], end[0] - start[0])
                turn += abs(math.remainder(next_heading - heading, math.tau))
                heading = next_heading
                distance += segment
        total_seconds = distance / MOCK_SPEED_MPS + turn / MOCK_TURN_RAD_PER_S + wait_seconds
        return f'약 {distance:.1f} m, 모의 주행 속도 기준 약 {total_seconds:.1f}초입니다.'

    def explanation(self):
        """LLM 없이 현재 상태와 기록만으로 설명을 생성합니다."""
        messages = {'idle': '모의 로봇이 이동 명령을 기다리고 있습니다.' if self.navigation_map else REASONS['map_required'],
                    'moving': f'{self.target} 지점으로 지도 경로를 따라 모의 이동 중입니다.',
                    'waiting': f'경유지에서 대기 중입니다. 약 {self.wait_remaining_s:.1f}초 후 {self.target} 지점으로 이동합니다.',
                    'arrived': '목적지의 모의 좌표에 도착했습니다.',
                    'stopped': '모의 주행이 정지되어 있습니다. 재출발하려면 이동 명령이 필요합니다.'}
        evidence = next((e for e in self.events if e['type'] in {
            'initialized', 'map_loaded', 'initial_pose_set', 'move_started', 'wait_started', 'wait_ended', 'stopped', 'arrived',
        }), self.events[0])
        message = messages[self.status]
        if self.status == 'stopped':
            message = evidence['message'] + ' 조건을 해제해도 자동 재출발하지 않습니다.'
        active = self._blocked_reason()
        if active:
            message += ' 현재 조건: ' + REASONS[active]
        return {'source': 'state_and_events', 'message': message,
                'evidence_event_id': evidence['id'], 'active_block': active}

    def snapshot(self):
        with self.lock:
            return deepcopy({'mode': 'mock', 'robot_connected': False, 'status': self.status,
                    'position': self.position, 'heading_rad': self.heading_rad, 'target': self.target,
                    'destinations': self.destinations, 'history': list(self.history),
                    'sensors': dict.fromkeys(['front', 'rear', 'left', 'right']),
                    'conditions': self.conditions, 'events': list(self.events),
                    'explanation': self.explanation(), 'planner': self.planner,
                    'navigation_map': self.navigation_map.model_dump(exclude_none=True) if self.navigation_map else None,
                    'footprint_blocked_cells': self.navigation_map.footprint_blocked_cells if self.navigation_map else (),
                    'map_id': self.map_id,
                    'route': list(self.waypoints),
                    'localization': ('simulated' if self.conditions['localization_valid'] else 'lost')
                    if self.navigation_map else 'unavailable',
                    'camera_url': None, 'depth_url': None})
