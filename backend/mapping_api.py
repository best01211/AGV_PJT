import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException

from mapping.models import MapConfig, ObservationBatch
from mapping.occupancy_grid import OccupancyGrid
from mapping.map_storage import MapStorage

router = APIRouter(prefix='/api/mapping', tags=['experimental sensor mapping'])
lock = threading.RLock()
grid = None
storage = MapStorage(Path(__file__).resolve().parents[1] / 'data' / 'maps')


def require_grid():
    if grid is None:
        raise HTTPException(409, '먼저 실제 센서 장착값으로 지도를 초기화해 주세요.')
    return grid


@router.post('/initialize')
def initialize(config: MapConfig):
    global grid
    with lock:
        if grid is not None:
            raise HTTPException(409, '이미 초기화된 지도입니다. 교체하려면 reset API를 사용하세요.')
        grid = OccupancyGrid(config)
        return grid.snapshot()


@router.post('/reset')
def reset(config: MapConfig):
    global grid
    with lock:
        grid = OccupancyGrid(config)
        return grid.snapshot()


@router.get('/state')
def state():
    with lock:
        return require_grid().snapshot()


@router.post('/observations')
def observations(batch: ObservationBatch):
    with lock:
        return require_grid().integrate(batch)


@router.post('/save')
def save():
    with lock:
        return {'map_id': storage.save(require_grid())}


@router.post('/load/{map_id}')
def load(map_id: str):
    global grid
    with lock:
        grid = read_saved_map(map_id)
        return grid.snapshot()


def read_saved_map(map_id: str):
    try:
        return storage.load(map_id)
    except FileNotFoundError as error:
        raise HTTPException(404, '저장된 지도가 없습니다.') from error
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, '지도 ID 또는 저장 데이터가 올바르지 않습니다.') from error
