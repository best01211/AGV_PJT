import asyncio
import json
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response

from backend.camera import CameraConfig, CameraService

router = APIRouter(prefix='/api/camera', tags=['camera'])
camera = CameraService()
secondary_camera = CameraService(CameraConfig(
    os.getenv('JETANK_SECONDARY_CAMERA_URL', '').strip(),
    os.getenv('JETANK_SECONDARY_CAMERA_FORMAT', '').strip().lower(),
))
project_root = Path(__file__).resolve().parents[1]
capture_setting = os.getenv('JETANK_C270_CAPTURE_DIR')
capture_dir = Path(capture_setting) if capture_setting else None
capture_lock = asyncio.Lock()
range_lock = asyncio.Lock()


def saved_frames():
    return sorted(capture_dir.glob('view-*.jpg'))


@router.get('/calibration-captures')
async def calibration_captures():
    return {'enabled': capture_dir is not None,
            'count': len(saved_frames()) if capture_dir is not None else 0}


def save_calibration_frame(jpeg, previous):
    capture_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=capture_dir, suffix='.jpg', delete=False) as file:
        pending = Path(file.name)
        file.write(jpeg)
    try:
        python = project_root / '.depth-venv' / 'Scripts' / 'python.exe'
        result = subprocess.run(
            [str(python), '-m', 'depth.calibrate_c270', '--check-last',
             *map(str, previous), str(pending)],
            cwd=project_root, capture_output=True, timeout=15)
        if result.returncode == 1:
            if b'nearly identical' in result.stdout.strip().rsplit(b'\n', 1)[-1]:
                raise HTTPException(422, '이전 사진과 위치가 거의 같습니다. 마커판을 옮기거나 기울여 주세요.')
            raise HTTPException(422, '마커 12개가 모두 선명하게 보여야 저장됩니다.')
        if result.returncode:
            raise HTTPException(503, '촬영 프레임을 검사할 수 없습니다. 영상 해상도를 확인해 주세요.')
        pending.replace(capture_dir / f'view-{uuid4().hex}.jpg')
    finally:
        pending.unlink(missing_ok=True)


@router.post('/calibration-captures')
async def capture_calibration_frame():
    if capture_dir is None:
        raise HTTPException(404, 'C270 영상 화면에서 촬영해 주세요.')
    if capture_lock.locked():
        raise HTTPException(409, '이전 촬영을 저장하고 있습니다.')
    if not camera.snapshot()['frame_available']:
        raise HTTPException(503, '최신 영상이 없습니다. 영상 연결을 확인해 주세요.')
    jpeg = camera.frame
    async with capture_lock:
        previous = saved_frames()
        if len(previous) >= 12:
            return {'enabled': True, 'count': len(previous)}
        try:
            await asyncio.to_thread(save_calibration_frame, jpeg, previous)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HTTPException(503, '촬영 프레임을 저장할 수 없습니다.') from error
        return {'enabled': True, 'count': len(previous) + 1}


@router.get('/status')
async def status():
    return camera.snapshot()


@router.post('/connect')
async def connect():
    return await start_camera(camera)


async def start_camera(receiver):
    try:
        return await receiver.start()
    except ValueError as error:
        raise HTTPException(409, str(error))


@router.post('/disconnect')
async def disconnect():
    return await camera.stop()


@router.get('/frame')
async def frame():
    return camera_frame(camera)


def camera_frame(receiver):
    if not receiver.snapshot()['frame_available']:
        raise HTTPException(503, '수신된 최신 영상 프레임이 없습니다.')
    return Response(receiver.frame, media_type='image/jpeg', headers={
        'Cache-Control': 'no-store', 'X-Frame-Sequence': str(receiver.sequence),
        'X-Content-Type-Options': 'nosniff'})


@router.get('/secondary/status')
async def secondary_status():
    return secondary_camera.snapshot()


@router.post('/secondary/connect')
async def secondary_connect():
    return await start_camera(secondary_camera)


@router.post('/secondary/disconnect')
async def secondary_disconnect():
    return await secondary_camera.stop()


@router.get('/secondary/frame')
async def secondary_frame():
    return camera_frame(secondary_camera)


@router.post('/board-range')
async def board_range():
    calibration = os.getenv('JETANK_CAMERA_CALIBRATION')
    if capture_dir is None or not calibration or not Path(calibration).is_file():
        raise HTTPException(503, 'C270 보정 파일이 필요합니다.')
    if range_lock.locked():
        raise HTTPException(409, '거리 측정 중입니다.')
    if not camera.snapshot()['frame_available']:
        raise HTTPException(503, '최신 영상이 없습니다. 영상 연결을 확인해 주세요.')
    jpeg, sequence = camera.frame, camera.sequence
    python = project_root / '.depth-venv' / 'Scripts' / 'python.exe'
    async with range_lock:
        try:
            result = await asyncio.to_thread(
                subprocess.run, [str(python), '-m', 'depth.c270_board_range', calibration],
                input=jpeg, cwd=project_root, capture_output=True, check=True, timeout=5)
            measurement = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise HTTPException(503, 'C270 거리 계산을 실행할 수 없습니다.') from error
    if measurement.get('error') == 'markers':
        raise HTTPException(422, f"마커 {measurement['count']}/12개 인식. 판 전체를 화면에 넣고 줄자를 치워 주세요.")
    if measurement.get('error'):
        raise HTTPException(422, '640×480 원본 영상과 마커판의 위치를 확인해 주세요.')
    return {**measurement, 'sequence': sequence}
