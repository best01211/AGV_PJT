"""Read-only navigation data supplied by the mapping team."""

import heapq
import math
import re
from functools import cached_property
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai.command_parser import START_POINT, normalize_name
from backend.agv_geometry import AGV_TURN_RADIUS_M

Cell = tuple[int, int]
Point = tuple[float, float]
DESTINATION_NAME_PATTERN = r'^[A-Za-z0-9가-힣]+(?: [A-Za-z0-9가-힣]+)*$'
Occupancy = Annotated[int, Field(strict=True, ge=-1, le=100)]
StartHeading = Annotated[int, Field(strict=True, ge=0, lt=360)]


class NavigationMap(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    schema_version: Literal[1]
    frame_id: str = Field(min_length=1, max_length=60)
    resolution_m: float = Field(ge=0.02, le=1)
    origin_x_m: float = Field(default=0, ge=-10000, le=10000)
    origin_y_m: float = Field(default=0, ge=-10000, le=10000)
    cols: int = Field(gt=0, le=40000)
    rows: int = Field(gt=0, le=40000)
    data: list[Occupancy] = Field(min_length=1, max_length=40000)
    start: Cell
    start_position_m: Point | None = None
    start_heading_deg: StartHeading = 90
    destinations: dict[str, Cell] = Field(max_length=50)
    zones: dict[str, list[Cell]] = Field(default_factory=dict, max_length=50)
    # Sensor conversions preserve the floor evidence under cells marked as clearance (75).
    clearance_radius_m: float | None = Field(default=None, gt=0, le=15)

    @model_validator(mode='after')
    def validate_map(self):
        if len(self.data) != self.cols * self.rows:
            raise ValueError('data length must match cols × rows')
        names = set()
        for name, cell in self.destinations.items():
            normalized = normalize_name(name)
            if not re.fullmatch(DESTINATION_NAME_PATTERN, name) or len(name) > 40:
                raise ValueError('invalid destination name')
            if normalized in names:
                raise ValueError('destination names must be unique')
            names.add(normalized)
            if not self.is_free(cell):
                raise ValueError(f'destination must be on a free cell: {name}')
        if not self.is_free(self.start):
            raise ValueError('start must be on a free cell')
        if self.start_position_m is not None and self.cell_at(self.start_position_m) != self.start:
            raise ValueError('start_position_m must be inside the start cell')
        occupied = set()
        for name, cells in self.zones.items():
            members = set(cells)
            if len(members) < 2 or len(members) != len(cells):
                raise ValueError('구역에는 서로 다른 칸이 두 개 이상 필요합니다.')
            if self.destinations.get(name) not in members:
                raise ValueError('구역에는 해당 장소의 도착 지점이 포함되어야 합니다.')
            if any(not self.is_free(cell) for cell in members) or occupied.intersection(members):
                raise ValueError('구역은 통행 가능한 칸에만 지정할 수 있으며 서로 겹칠 수 없습니다.')
            occupied.update(members)
        return self

    def is_free(self, cell):
        col, row = cell
        return 0 <= col < self.cols and 0 <= row < self.rows and self.data[row * self.cols + col] == 0

    def center(self, cell):
        col, row = cell
        return [self.origin_x_m + (col + 0.5) * self.resolution_m,
                self.origin_y_m + (row + 0.5) * self.resolution_m]

    @property
    def start_position(self):
        return self.center(self.start) if self.start_position_m is None else list(self.start_position_m)

    def is_start_destination(self, name):
        return (self.destinations.get(name) == self.start and name not in self.zones
                and normalize_name(name).replace(' ', '') in ('시작', START_POINT))

    def destination_position(self, name):
        if (name == START_POINT and name not in self.destinations) or self.is_start_destination(name):
            return self.start_position
        return self.center(self.destinations[name])

    def _grid_point(self, point):
        return ((point[0] - self.origin_x_m) / self.resolution_m,
                (point[1] - self.origin_y_m) / self.resolution_m)

    def cell_at(self, point):
        x, y = self._grid_point(point)
        if 0 <= x < self.cols and 0 <= y < self.rows:
            return math.floor(x), math.floor(y)
        return None

    def connect_positions(self, start, end):
        """Connect an off-center position to the cell-center route using axis-aligned legs."""
        def clear(a, b):
            return self.footprint_clear(self._grid_point(a), self._grid_point(b))

        if start[0] == end[0] or start[1] == end[1]:
            return [end] if clear(start, end) else None
        for corner in ([end[0], start[1]], [start[0], end[1]]):
            if clear(start, corner) and clear(corner, end):
                return [corner, end]
        return None

    def can_start_at(self, cell, position):
        return (cell is not None and self.is_driveable(cell)
                and self.connect_positions(position, self.center(cell)) is not None)

    @cached_property
    def obstacle_row_prefix(self):
        stride = self.cols + 1
        prefix = [0] * (stride * self.rows)
        for row in range(self.rows):
            count = 0
            for col in range(self.cols):
                value = self.data[row * self.cols + col]
                count += value != 0 and (value != 75 or self.clearance_radius_m is None)
                prefix[row * stride + col + 1] = count
        return prefix

    def footprint_clear(self, start: Point, end: Point):
        """Check the 20 cm body through a turn or sweep in grid coordinates."""
        radius = max(self.clearance_radius_m or 0, AGV_TURN_RADIUS_M) / self.resolution_m
        x0, x1 = sorted((start[0], end[0]))
        y0, y1 = sorted((start[1], end[1]))
        if x0 - radius <= 0 or y0 - radius <= 0 or x1 + radius >= self.cols or y1 + radius >= self.rows:
            return False
        radius_squared = radius ** 2
        stride = self.cols + 1
        prefix = self.obstacle_row_prefix
        for row in range(math.ceil(y0 - radius) - 1, math.floor(y1 + radius) + 1):
            dy = max(row - y1, y0 - row - 1, 0)
            reach = math.sqrt(max(0, radius_squared - dy * dy))
            first = math.ceil(x0 - reach) - 1
            last = math.floor(x1 + reach)
            if prefix[row * stride + last + 1] != prefix[row * stride + first]:
                return False
        return True

    @cached_property
    def driveable_cells(self):
        safe = bytearray(len(self.data))
        for index, value in enumerate(self.data):
            if value == 0:
                col, row = index % self.cols, index // self.cols
                center = (col + .5, row + .5)
                safe[index] = self.footprint_clear(center, center)
        return safe

    def is_driveable(self, cell):
        col, row = cell
        return 0 <= col < self.cols and 0 <= row < self.rows and bool(self.driveable_cells[row * self.cols + col])

    @cached_property
    def footprint_blocked_cells(self):
        return tuple(i for i, value in enumerate(self.data)
                     if (value == 0 and not self.driveable_cells[i])
                     or (value == 75 and self.clearance_radius_m is not None))

    def _line_clear(self, start: Point, goal: Point):
        """Check every cell touched by a segment, including both sides of a corner."""
        col, row = map(math.floor, start)
        end_col, end_row = map(math.floor, goal)
        dx, dy = goal[0] - start[0], goal[1] - start[1]
        dc = 1 if dx > 0 else -1
        dr = 1 if dy > 0 else -1
        next_col = (col + (dc > 0) - start[0]) / dx if dx else math.inf
        next_row = (row + (dr > 0) - start[1]) / dy if dy else math.inf
        col_step = abs(1 / dx) if dx else math.inf
        row_step = abs(1 / dy) if dy else math.inf

        while self.is_free((col, row)):
            if (col, row) == (end_col, end_row):
                return True
            if math.isclose(next_col, next_row, rel_tol=0, abs_tol=1e-10):
                if not self.is_free((col + dc, row)) or not self.is_free((col, row + dr)):
                    return False
                col += dc
                row += dr
                next_col += col_step
                next_row += row_step
            elif next_col < next_row:
                col += dc
                next_col += col_step
            else:
                row += dr
                next_row += row_step
        return False

    def any_angle_route(self, position, goal: Cell):
        """Find a collision-free polyline with Theta* and Euclidean costs."""
        origin = self._grid_point(position)
        start = tuple(map(math.floor, origin))
        if not self.is_free(start) or not self.is_free(goal):
            return None
        goal_point = (goal[0] + 0.5, goal[1] + 0.5)
        if self._line_clear(origin, goal_point):
            return [self.center(goal)]

        def point(cell):
            return origin if cell == start else (cell[0] + 0.5, cell[1] + 0.5)

        frontier = [(math.dist(origin, goal_point), 0.0, start)]
        costs = {start: 0.0}
        parents = {start: start}
        while frontier:
            _, cost, cell = heapq.heappop(frontier)
            if cost != costs[cell]:
                continue
            if cell == goal:
                path = []
                while cell != start:
                    path.append(self.center(cell))
                    cell = parents[cell]
                return path[::-1]
            col, row = cell
            for dc, dr in ((-1, 0), (1, 0), (0, -1), (0, 1),
                           (-1, -1), (-1, 1), (1, -1), (1, 1)):
                neighbor = (col + dc, row + dr)
                if neighbor == start or not self.is_free(neighbor):
                    continue
                end = point(neighbor)
                parent = parents[cell]
                if not self._line_clear(point(parent), end):
                    parent = cell
                    if not self._line_clear(point(cell), end):
                        continue
                new_cost = costs[parent] + math.dist(point(parent), end)
                if new_cost < costs.get(neighbor, math.inf):
                    costs[neighbor] = new_cost
                    parents[neighbor] = parent
                    heapq.heappush(frontier, (new_cost + math.dist(end, goal_point), new_cost, neighbor))
        return None

    def route(self, start, goal):
        """Return a shortest 4-connected path, including both endpoints."""
        if not self.is_driveable(start) or not self.is_driveable(goal):
            return None
        frontier = [(0, 0, start)]
        costs = {start: 0}
        parents = {}
        while frontier:
            _, cost, cell = heapq.heappop(frontier)
            if cost != costs[cell]:
                continue
            if cell == goal:
                path = [cell]
                while cell != start:
                    cell = parents[cell]
                    path.append(cell)
                return path[::-1]
            col, row = cell
            for neighbor in ((col - 1, row), (col + 1, row), (col, row - 1), (col, row + 1)):
                next_cost = cost + 1
                if not self.is_driveable(neighbor) or next_cost >= costs.get(neighbor, float('inf')):
                    continue
                costs[neighbor] = next_cost
                parents[neighbor] = cell
                heuristic = abs(neighbor[0] - goal[0]) + abs(neighbor[1] - goal[1])
                heapq.heappush(frontier, (next_cost + heuristic, next_cost, neighbor))
        return None
