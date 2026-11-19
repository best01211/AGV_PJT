import asyncio
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

from backend.service import RobotService, TRIAL_FRAME_ID
from backend.navigation import Cell, DESTINATION_NAME_PATTERN, NavigationMap, Point, StartHeading
from backend.mapping_api import read_saved_map, router as mapping_router
from backend.sensor_navigation import Footprint, SensorMapRegistration, navigation_preview, registered_navigation
from backend import camera_api
from backend import depth_api

if os.getenv('JETANK_MODE', 'mock') != 'mock':
    raise RuntimeError('현재는 mock 모드만 지원합니다. 하드웨어 프로토콜 확인이 필요합니다.')

service = RobotService()


@asynccontextmanager
async def lifespan(app):
    async def update():
        while True:
            service.tick()
            await asyncio.sleep(0.1)
    task = asyncio.create_task(update())
    try:
        yield
    finally:
        await depth_api.depth_service.stop()
        await camera_api.camera.stop()
        await camera_api.secondary_camera.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='JETANK Mock Control', lifespan=lifespan)
app.include_router(mapping_router)
app.include_router(camera_api.router)
app.include_router(depth_api.router)
frontend = Path(__file__).resolve().parents[1] / 'frontend'
# ES modules require a JavaScript MIME type. Windows may associate .js with text/plain.
mimetypes.add_type('text/javascript', '.js')
app.mount('/static', StaticFiles(directory=frontend), name='static')
app.mount('/about', StaticFiles(directory=frontend.parent / 'sites' / 'rove-intro' / 'dist', html=True), name='about')


class Command(BaseModel):
    text: str = Field(min_length=1, max_length=200)


class TrialMotion(BaseModel):
    map_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    action: Literal['forward', 'left', 'right']


class MockConditions(BaseModel):
    obstacle: StrictBool
    localization_valid: StrictBool


class DestinationRegistration(BaseModel):
    map_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    name: str = Field(max_length=40, pattern=DESTINATION_NAME_PATTERN)
    cell: Cell
    zone_cells: list[Cell] | None = Field(default=None, min_length=2, max_length=40000)


class InitialPose(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    map_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    position_m: Point | None = None
    cell: Cell | None = None
    heading_deg: StartHeading

    @model_validator(mode='after')
    def exactly_one_position(self):
        if (self.position_m is None) == (self.cell is None):
            raise ValueError('position_m 또는 cell 중 하나를 입력하세요.')
        return self


@app.get('/')
def index():
    return FileResponse(frontend / 'index.html')


@app.get('/mapping')
def mapping_page():
    return FileResponse(frontend / 'mapping.html')


@app.get('/video')
def video_page():
    return FileResponse(frontend / 'video.html')


@app.get('/api/state')
def state():
    return service.snapshot()


@app.post('/api/commands')
def command(command: Command):
    return service.command(command.text)


@app.post('/api/trial/motion')
def trial_motion(command: TrialMotion):
    try:
        return service.start_trial_motion(command.map_id, command.action)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put('/api/navigation/map')
def load_navigation_map(navigation_map: NavigationMap):
    try:
        return service.load_navigation_map(navigation_map)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.put('/api/navigation/initial-pose')
def set_initial_pose(pose: InitialPose):
    try:
        return service.set_initial_pose(pose.map_id, pose.cell, pose.heading_deg, position_m=pose.position_m)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def preview_saved_map(map_id: str, footprint: Footprint):
    grid = read_saved_map(map_id)
    if grid.config.frame_id == TRIAL_FRAME_ID:
        raise HTTPException(422, '이 좌표계 이름은 1m 예비 주행 지도에서 사용합니다.')
    try:
        return navigation_preview(grid, footprint)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.post('/api/navigation/sensor-maps/{map_id}/preview')
def preview_sensor_map(map_id: str, footprint: Footprint):
    return preview_saved_map(map_id, footprint)


@app.post('/api/navigation/sensor-maps/{map_id}/register')
def register_sensor_map(map_id: str, registration: SensorMapRegistration):
    preview = preview_saved_map(map_id, registration)
    try:
        navigation = registered_navigation(preview, registration)
        return service.load_navigation_map(navigation)
    except ValidationError as error:
        raise HTTPException(422, '시작점과 목적지는 서로 구분되는 이름과 통행 가능 칸을 사용해야 합니다.') from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.post('/api/navigation/destinations')
def add_destination(destination: DestinationRegistration):
    try:
        return service.add_destination(destination.map_id, destination.name, destination.cell, destination.zone_cells)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.delete('/api/navigation/destinations/{name}')
def remove_destination(name: str, map_id: str):
    try:
        return service.remove_destination(map_id, name)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post('/api/stop')
def stop():
    return service.command('정지해')


@app.post('/api/mock/conditions')
def mock_conditions(conditions: MockConditions):
    return service.set_mock_conditions(conditions.obstacle, conditions.localization_valid)
