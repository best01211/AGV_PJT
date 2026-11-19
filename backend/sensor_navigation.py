"""Derive a conservative, simulation-only navigation grid from a saved observation map."""

import math

from pydantic import Field

from backend.agv_geometry import AGV_WIDTH_M, AGV_LENGTH_M
from backend.navigation import Cell, NavigationMap, Point, StartHeading
from mapping.models import Contract

FREE, UNKNOWN, UNCERTAIN, CLEARANCE, OCCUPIED = 0, -1, 50, 75, 100


class Footprint(Contract):
    width_m: float = Field(default=AGV_WIDTH_M, gt=0, le=10)
    length_m: float = Field(default=AGV_LENGTH_M, gt=0, le=10)
    margin_m: float = Field(ge=0, le=5)


class SensorMapRegistration(Footprint):
    start: Cell
    start_position_m: Point | None = None
    start_heading_deg: StartHeading = 90
    destinations: dict[str, Cell] = Field(min_length=1, max_length=50)


def navigation_preview(grid, footprint: Footprint):
    snapshot = grid.snapshot()
    config = grid.config
    cols, rows = grid.cols, grid.rows
    if abs(config.origin_x_m) > 10000 or abs(config.origin_y_m) > 10000:
        raise ValueError('관측 지도의 원점이 주행 지도에서 지원하는 범위를 벗어났습니다.')
    resolution = config.resolution_m
    radius = math.hypot(max(AGV_WIDTH_M, footprint.width_m),
                        max(AGV_LENGTH_M, footprint.length_m)) / 2 + footprint.margin_m
    padding = math.ceil(radius / resolution)
    evidence = [
        UNKNOWN if cell < 0 else FREE if cell <= 35 else OCCUPIED if cell >= 65 else UNCERTAIN
        for cell in snapshot['data']
    ]
    free_evidence_cells = evidence.count(FREE)

    # Summed-area table counts blocked cells within a conservative square around
    # every candidate cell. This also covers positions between cell centers.
    stride = cols + 1
    blocked = [0] * ((rows + 1) * stride)
    for row in range(rows):
        running = 0
        for col in range(cols):
            running += evidence[row * cols + col] != FREE
            offset = row * stride + col + 1
            blocked[offset + stride] = blocked[offset] + running

    for row in range(rows):
        for col in range(cols):
            index = row * cols + col
            if evidence[index] != FREE:
                continue
            x0, y0 = col - padding, row - padding
            x1, y1 = col + padding + 1, row + padding + 1
            if x0 < 0 or y0 < 0 or x1 > cols or y1 > rows:
                evidence[index] = CLEARANCE
                continue
            count = (blocked[y1 * stride + x1] - blocked[y0 * stride + x1]
                     - blocked[y1 * stride + x0] + blocked[y0 * stride + x0])
            if count:
                evidence[index] = CLEARANCE

    return {
        'frame_id': config.frame_id, 'resolution_m': resolution,
        'origin_x_m': config.origin_x_m, 'origin_y_m': config.origin_y_m,
        'cols': cols, 'rows': rows, 'data': evidence,
        'clearance_radius_m': radius, 'clearance_cells': padding,
        'free_evidence_cells': free_evidence_cells,
        'driveable_cells': evidence.count(FREE),
        'free_updates_enabled': config.enable_free_updates,
    }


def registered_navigation(preview, registration: SensorMapRegistration):
    fields = ('frame_id', 'resolution_m', 'origin_x_m', 'origin_y_m', 'cols', 'rows', 'data', 'clearance_radius_m')
    navigation = NavigationMap(
        **{field: preview[field] for field in fields},
        schema_version=1, start=registration.start, start_heading_deg=registration.start_heading_deg,
        start_position_m=registration.start_position_m,
        destinations=registration.destinations,
    )
    reachable = {navigation.start}
    frontier = [navigation.start]
    while frontier:
        col, row = frontier.pop()
        for neighbor in ((col - 1, row), (col + 1, row), (col, row - 1), (col, row + 1)):
            if neighbor not in reachable and navigation.is_driveable(neighbor):
                reachable.add(neighbor)
                frontier.append(neighbor)
    unreachable = [name for name, cell in navigation.destinations.items() if cell not in reachable]
    if unreachable:
        raise ValueError('시작점에서 도달할 수 없는 목적지: ' + ', '.join(unreachable))
    return navigation
