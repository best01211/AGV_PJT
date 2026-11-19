from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, ConfigDict
import asyncio
import csv
import os
import json
import re
import statistics
from tempfile import TemporaryDirectory
from pathlib import Path
from uuid import uuid4
from typing import Literal

from backend import camera_api
from backend.depth_service import DepthService
from depth.roi import summarize_region

router = APIRouter(prefix='/api/depth',tags=['calibrated depth'])
depth_service = DepthService()
MEASUREMENT_DIR = Path(os.getenv('JETANK_DEPTH_MEASUREMENT_DIR',str(Path(__file__).resolve().parents[1]/'data/depth_evaluation')))
BATCH_LOCK = asyncio.Lock()
CAPTURE_ID = re.compile(r'([0-9a-f]{32}):([1-9][0-9]*)')


class Measurement(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)
    scene: str = Field(min_length=1,max_length=60)
    ground_truth_m: float = Field(gt=0,le=20)
    sequence: int = Field(ge=0)
    run_id: int = Field(ge=1)


class PinRequest(BaseModel):
    run_id:int=Field(ge=1)
    sequence:int=Field(ge=0)


class TargetRegion(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token:str=Field(pattern=r'^[a-f0-9]{32}$')
    x:int=Field(ge=0,strict=True)
    y:int=Field(ge=0,strict=True)
    width:int=Field(ge=4,strict=True)
    height:int=Field(ge=4,strict=True)


class TargetMeasurement(TargetRegion):
    scene:str=Field(min_length=1,max_length=60)
    ground_truth_m:float=Field(gt=0,le=20,allow_inf_nan=False)
    distance_kind:Literal['camera_ray_range','optical_z']='camera_ray_range'


def require_pin(token):
    entry=depth_service.get_pin(token)
    if entry is None:
        raise HTTPException(410,'고정 프레임의 보관 시간(2분)이 지났습니다. 표적을 다시 선택해 주세요.')
    return entry


def target_summary(entry,data):
    if 'depth_values' not in entry['images']:
        raise HTTPException(409,'Depth를 종료 후 다시 시작해 표적 측정을 활성화해 주세요.')
    try:
        return summarize_region(entry['images']['depth_values'],entry['images']['valid_values'],
                                entry['result'].get('image_width',300),entry['result'].get('image_height',300),
                                [data.x,data.y,data.width,data.height],entry['result'].get('camera_matrix'))
    except ValueError as error:
        raise HTTPException(422,str(error))


@router.post('/pin')
async def pin(data:PinRequest):
    token=depth_service.pin_result(data.run_id,data.sequence)
    if token is None:
        raise HTTPException(409,'프레임이 갱신됐습니다. 다시 선택해 주세요.')
    return {'token':token,'sequence':data.sequence,'expires_in_s':120}


@router.get('/pinned/{token}/{kind}')
async def pinned_frame(token:str,kind:str):
    if kind not in ('undistorted','depth'):
        raise HTTPException(404,'Unknown image')
    entry=require_pin(token)
    return Response(entry['images'][kind],media_type='image/jpeg',headers={'Cache-Control':'no-store'})


@router.post('/probe')
async def probe(data:TargetRegion):
    entry=require_pin(data.token)
    return dict(target_summary(entry,data),sequence=entry['result']['sequence'])


@router.post('/target-measurements')
async def target_measurement(data:TargetMeasurement):
    entry=require_pin(data.token)
    summary=target_summary(entry,data)
    prediction=summary['model_ray_range_m'] if data.distance_kind=='camera_ray_range' else summary['model_depth_z_m']
    if prediction is None:
        raise HTTPException(409,'선택 영역에 유효한 깊이가 부족합니다.')
    sample_id=uuid4().hex
    folder=MEASUREMENT_DIR
    folder.mkdir(parents=True,exist_ok=True)
    for suffix,key in [('.npz','array'),('_raw.jpg','raw'),('_corrected.jpg','undistorted')]:
        (folder/(sample_id+suffix)).write_bytes(entry['images'][key])
    result=entry['result']
    row={'sample_id':sample_id,'scene':data.scene,'ground_truth_m':data.ground_truth_m,
         'prediction_m':prediction,'sequence':result['sequence'],'run_id':depth_service.run_id,
         'encoder':result['encoder'],'input_size':result['input_size'],'roi':json.dumps(summary['roi']),
         'measurement_kind':'selected_target','metric_validated':False,
         'distance_kind':data.distance_kind,'prediction_depth_z_m':summary['model_depth_z_m'],
         'prediction_ray_range_m':summary['model_ray_range_m']}
    target=folder/'measurements_v4.csv'
    exists=target.exists()
    with target.open('a',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    (folder/(sample_id+'.json')).write_text(json.dumps(dict(row,region_summary=summary,calibration_id=result.get('calibration_id'),
                                                           profile_id=result.get('profile_id'),
                                                           focal_px_used=result.get('focal_px_used'),
                                                           estimated_intrinsics=bool(result.get('preview'))),ensure_ascii=False,indent=2),encoding='utf-8')
    return row


@router.get('/status')
async def status():
    return depth_service.snapshot()


def write_batch(batch, entries):
    folder = MEASUREMENT_DIR / 'batches'
    folder.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=folder) as temporary:
        staged = Path(temporary) / batch['batch_id']
        staged.mkdir()
        for entry, frame in zip(entries, batch['frames']):
            prefix = str(frame['sequence'])
            for suffix, key in [('.npz','array'), ('_raw.jpg','raw'), ('_corrected.jpg','undistorted')]:
                (staged / (prefix + suffix)).write_bytes(entry['images'][key])
        (staged / 'measurement.json').write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding='utf-8')
        staged.rename(folder / batch['batch_id'])


@router.post('/target-batches')
async def target_batch(data: TargetMeasurement):
    if data.distance_kind != 'camera_ray_range':
        raise HTTPException(422, '묶음 기록은 렌즈 직선거리만 지원합니다.')
    if BATCH_LOCK.locked():
        raise HTTPException(409, '다른 묶음 기록이 진행 중입니다.')

    async with BATCH_LOCK:
        anchor = require_pin(data.token)['result']
        capture = CAPTURE_ID.fullmatch(anchor.get('capture_id') or '')
        if capture is None:
            raise HTTPException(409, '촬영 ID를 보내는 C270 송출 버전이 필요합니다.')
        stream, last_capture = capture[1], int(capture[2])
        run_id = depth_service.run_id
        last_sequence = depth_service.result['sequence']
        entries, frames = [], []
        deadline = asyncio.get_running_loop().time() + 25
        while len(frames) < 8:
            if depth_service.run_id != run_id or depth_service.state != 'running':
                raise HTTPException(409, 'Depth가 중단됐습니다. 표적을 다시 고정하세요.')
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(504, '25초 안에 유효한 새 촬영 프레임 8장을 받지 못했습니다.')
            await asyncio.sleep(.05)
            result = depth_service.result
            if result is None or result['sequence'] <= last_sequence:
                continue
            last_sequence = result['sequence']
            entry = depth_service.get_result(run_id, last_sequence)
            if entry is None or not depth_service.snapshot()['result_available']:
                continue
            capture = CAPTURE_ID.fullmatch(result.get('capture_id') or '')
            if capture is None or capture[1] != stream:
                raise HTTPException(409, '카메라 송출 세션이 바뀌었습니다. 표적을 다시 고정하세요.')
            if int(capture[2]) <= last_capture:
                continue
            last_capture = int(capture[2])
            if any(result.get(key) != anchor.get(key) for key in
                   ('profile_id','calibration_id','camera_matrix','image_width','image_height')):
                raise HTTPException(409, '카메라 설정이 바뀌었습니다. 표적을 다시 고정하세요.')
            summary = target_summary(entry, data)
            prediction = summary['model_ray_range_m']
            if prediction is None:
                continue
            entries.append(entry)
            frames.append({'sequence':last_sequence, 'capture_id':result['capture_id'],
                           'prediction_m':prediction, 'region_summary':summary})

        values = [frame['prediction_m'] for frame in frames]
        batch = {'batch_id':uuid4().hex, 'run_id':run_id, 'anchor_sequence':anchor['sequence'],
                 'scene':data.scene, 'ground_truth_m':data.ground_truth_m,
                 'distance_kind':data.distance_kind, 'roi':[data.x,data.y,data.width,data.height],
                 'profile_id':anchor.get('profile_id'), 'calibration_id':anchor.get('calibration_id'),
                 'focal_px_used':anchor.get('focal_px_used'), 'metric_validated':False,
                 'frames':frames, 'median_m':statistics.median(values),
                 'min_m':min(values), 'max_m':max(values)}
        await asyncio.to_thread(write_batch, batch, entries)
        return batch


@router.post('/start')
async def start():
    try:
        return await depth_service.start(camera_api.camera)
    except ValueError as error:
        raise HTTPException(409,str(error))


@router.post('/stop')
async def stop():
    return await depth_service.stop()


@router.get('/frame/{kind}')
async def frame(kind:str, run_id:int|None=None, sequence:int|None=None):
    if kind not in ('depth','undistorted','array'):
        raise HTTPException(404,'알 수 없는 출력입니다.')
    if (run_id is None) != (sequence is None):
        raise HTTPException(422,'run_id와 sequence를 함께 지정하세요.')
    if run_id is not None:
        entry=depth_service.get_result(run_id,sequence)
        if entry is None:
            raise HTTPException(410,'표시할 프레임의 보관 시간이 지났습니다.')
        images, result = entry['images'],entry['result']
    else:
        if not depth_service.snapshot()['result_available']:
            raise HTTPException(503,'최신 Depth 결과가 없습니다.')
        images, result = depth_service.images,depth_service.result
    content_type='application/octet-stream' if kind=='array' else 'image/jpeg'
    headers={'Cache-Control':'no-store','X-Depth-Sequence':str(result['sequence']),
             'X-Depth-Run':str(depth_service.run_id)}
    if kind=='array':
        headers['Content-Disposition']='attachment; filename="depth_unvalidated.npz"'
    return Response(images[kind],media_type=content_type,headers=headers)


@router.post('/measurements')
async def measurement(data:Measurement):
    entry=depth_service.get_result(data.run_id,data.sequence)
    if entry is None:
        raise HTTPException(409,'프레임 보관 시간(최대 15초)이 지났거나 Depth가 재시작됐습니다. 최신 화면에서 기록해 주세요.')
    if entry['result'].get('preview'):
        raise HTTPException(409,'C270은 물체 영역 모드에서 실측 직선거리를 기록하세요.')
    prediction=entry['result']['summary']['model_center_m']
    if prediction is None:
        raise HTTPException(409,'중앙 영역에 유효한 추정값이 부족합니다.')
    folder=MEASUREMENT_DIR
    folder.mkdir(parents=True,exist_ok=True)
    sample_id=uuid4().hex
    (folder/(sample_id+'.npz')).write_bytes(entry['images']['array'])
    (folder/(sample_id+'_corrected.jpg')).write_bytes(entry['images']['undistorted'])
    (folder/(sample_id+'_raw.jpg')).write_bytes(entry['images']['raw'])
    row={'sample_id':sample_id,'scene':data.scene,'ground_truth_m':data.ground_truth_m,
         'prediction_m':prediction,'sequence':data.sequence,'run_id':data.run_id,'metric_validated':False}
    if 'encoder' in entry['result']:
        row.update(encoder=entry['result']['encoder'],input_size=entry['result']['input_size'],
                   profile_id=entry['result'].get('profile_id',''),
                   corrected_center_m=entry['result']['summary'].get('distance_correction',{}).get('corrected_center_m'))
    # Preserve old Small CSV and its schema; new identified model outputs use a separate ledger.
    target=folder/('measurements_v2.csv' if 'encoder' in row else 'measurements.csv')
    exists=target.exists()
    with target.open('a',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    return row
