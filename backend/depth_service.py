import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import secrets
from contextlib import asynccontextmanager
from collections import OrderedDict
from copy import deepcopy


ROOT = Path(__file__).resolve().parents[1]


class DepthService:
    def __init__(self):
        self.task = None
        self.process = None
        self.state = 'stopped'
        self.message = 'Depth 추론을 시작해 주세요.'
        self.result = None
        self.images = {}
        self.source_time = None
        self.run_id = 0
        self.log = None
        self.recent_results = OrderedDict()
        self.recent_bytes = 0
        self.pinned_results = OrderedDict()

    def pin_result(self, run_id, sequence):
        entry=self.get_result(run_id,sequence)
        if entry is None:
            return None
        now=time.monotonic()
        self.pinned_results=OrderedDict((key,value) for key,value in self.pinned_results.items() if now-value['pinned_at']<120)
        token=secrets.token_hex(16)
        self.pinned_results[token]={'entry':entry,'pinned_at':now,'run_id':run_id}
        while len(self.pinned_results)>4:
            self.pinned_results.popitem(last=False)
        return token

    def get_pin(self,token):
        item=self.pinned_results.get(token)
        if item is None:
            return None
        if time.monotonic()-item['pinned_at']>=120 or item['run_id']!=self.run_id or self.state!='running':
            self.pinned_results.pop(token,None)
            return None
        return item['entry']

    def _prune_results(self):
        now = time.monotonic()
        while self.recent_results:
            first = next(iter(self.recent_results.values()))
            if len(self.recent_results) <= 32 and self.recent_bytes <= 64 * 1024 * 1024 and now-first['source_time'] < 15:
                break
            removed = self.recent_results.popitem(last=False)[1]
            self.recent_bytes -= removed['bytes']

    def publish_result(self, output, images, source_time):
        key = (self.run_id, output['sequence'])
        entry = {'result':deepcopy(output), 'images':images.copy(), 'source_time':source_time,
                 'bytes':sum(len(value) for value in images.values())}
        previous = self.recent_results.pop(key, None)
        if previous:
            self.recent_bytes -= previous['bytes']
        self.recent_results[key] = entry
        self.recent_bytes += entry['bytes']
        self.images, self.result, self.source_time = images, output, source_time
        self._prune_results()

    def get_result(self, run_id, sequence):
        self._prune_results()
        if self.state != 'running' or run_id != self.run_id:
            return None
        return self.recent_results.get((run_id, sequence))

    def paths(self):
        engine=os.getenv('JETANK_DEPTH_ENGINE','depthpro')
        if engine=='depthpro':
            return (Path(os.getenv('JETANK_DEPTH_PYTHON',str(ROOT/'.depth-venv/Scripts/python.exe'))),
                    ROOT/'.depth-model/ml-depth-pro',ROOT/'.depth-model/checkpoints/depth_pro.pt')
        return (Path(os.getenv('JETANK_DEPTH_PYTHON', str(ROOT / '.depth-venv/Scripts/python.exe'))),
                ROOT / '.depth-model/Depth-Anything-V2',
                ROOT / '.depth-model/checkpoints/depth_anything_v2_metric_hypersim_vitb.pth')

    def preview_allowed(self):
        return os.getenv('JETANK_DEPTH_PREVIEW') == '1'

    def calibration_path(self):
        return Path(os.getenv('JETANK_CAMERA_CALIBRATION',str(ROOT/'config/camera_calibration.json')))

    def snapshot(self):
        age = None if self.source_time is None else time.monotonic() - self.source_time
        return {'state':self.state,'message':self.message,'result':self.result,
                'result_available': bool(self.images) and age is not None and age < 5 and self.state=='running',
                'source_age_s':None if age is None else round(age,2), 'run_id':self.run_id,
                'metric_validated':False,'usable_for_control':False,
                'nearfield_requirement':{'max_distance_m':1.0,'max_absolute_error_m':.01,'verified':False},
                'calibration_source':self.calibration_path().name,
                'calibration_available':self.calibration_path().is_file(),'preview_allowed':self.preview_allowed()}

    async def start(self, camera):
        if self.task and not self.task.done():
            return self.snapshot()
        python, repo, checkpoint = self.paths()
        engine=os.getenv('JETANK_DEPTH_ENGINE','depthpro')
        if engine not in ('depthpro','depthanything'):
            raise ValueError('JETANK_DEPTH_ENGINE must be depthpro or depthanything')
        if not self.calibration_path().is_file() and not (self.preview_allowed() and engine=='depthpro'):
            raise ValueError('카메라 보정 파일이 없습니다. 연결된 카메라로 체커보드 보정을 먼저 진행하세요.')
        if os.getenv('JETANK_CAMERA_CALIBRATION') and engine!='depthpro':
            raise ValueError('별도 카메라 보정에서는 Depth Pro를 사용하세요.')
        implementation=repo/('src/depth_pro' if engine=='depthpro' else 'metric_depth')
        if not python.is_file() or not implementation.is_dir() or not checkpoint.is_file():
            raise ValueError('Depth 실행 환경 또는 공식 모델 파일이 없습니다. DEPTH_INTEGRATION.md를 확인하세요.')
        await camera.start()
        # Browser state from before a server restart must not match a new inference session.
        self.run_id = secrets.randbelow(2**52 - 1) + 1
        self.recent_results.clear()
        self.pinned_results.clear()
        self.recent_bytes = 0
        self.images, self.result, self.source_time = {}, None, None
        self.state, self.message = 'loading', 'Metric 모델을 불러오는 중입니다.'
        self.task = asyncio.create_task(self._run(camera))
        return self.snapshot()

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await self._close_process()
        self.images, self.result, self.source_time = {}, None, None
        self.state, self.message = 'stopped', 'Depth 추론을 종료했습니다.'
        self.recent_results.clear()
        self.pinned_results.clear()
        self.recent_bytes = 0
        return self.snapshot()

    async def _close_process(self):
        if self.process:
            if self.process.returncode is None:
                self.process.terminate()
                await self.process.wait()
            self.process = None
        if self.log:
            self.log.close()
            self.log = None

    @asynccontextmanager
    async def _workspace(self, work):
        with tempfile.TemporaryDirectory(dir=work) as directory:
            try:
                yield Path(directory)
            finally:
                await self._close_process()

    async def _run(self, camera):
        python, repo, checkpoint = self.paths()
        work = ROOT/'tmp/depth-runtime'
        work.mkdir(parents=True, exist_ok=True)
        try:
            async with self._workspace(work) as folder:
                self.log = open(work/'worker.log','wb')
                self.process = await asyncio.create_subprocess_exec(
                    str(python), '-u','-m','depth.worker','--repo',str(repo),
                    '--engine',os.getenv('JETANK_DEPTH_ENGINE','depthpro'),
                    '--checkpoint',str(checkpoint),'--calibration',str(self.calibration_path()),
                    '--profile',str(ROOT/'config/depth_runtime_profile.json'),
                    *(['--preview'] if self.preview_allowed() else []),
                    cwd=str(ROOT), stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=self.log)
                ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), timeout=120))
                if not ready.get('ready'):
                    raise ValueError('worker startup failed')
                self.state, self.message = 'running', '보정 영상으로 Metric Depth 추론 중 · 거리 정확도 미검증'
                last_sequence = -3
                while True:
                    if not camera.snapshot()['frame_available']:
                        self.images = {}
                        self.message = '최신 카메라 프레임 대기 중입니다.'
                        await asyncio.sleep(.2)
                        continue
                    if camera.sequence - last_sequence < 3:
                        await asyncio.sleep(.05)
                        continue
                    sequence, image = camera.sequence, camera.frame
                    capture_id = camera.capture_id
                    source_time, source_received_at = camera.last_frame_monotonic, camera.last_frame_at
                    await asyncio.to_thread((folder/'input.jpg').write_bytes,image)
                    self.process.stdin.write((json.dumps({'input':str(folder/'input.jpg'),'output':str(folder),
                                                         'sequence':sequence,'source_received_at':source_received_at})+'\n').encode())
                    await self.process.stdin.drain()
                    line = await asyncio.wait_for(self.process.stdout.readline(), timeout=120)
                    output = json.loads(line)
                    if not output.get('ok'):
                        raise ValueError(output.get('error','inference failed'))
                    output['capture_id'] = capture_id
                    images = {'undistorted':(folder/'undistorted.jpg').read_bytes(),
                                   'raw':image,
                                   'depth':(folder/'depth.jpg').read_bytes(),
                                   'array':(folder/'latest_depth.npz').read_bytes()}
                    images['depth_values']=(folder/'depth_values.bin').read_bytes()
                    images['valid_values']=(folder/'valid_values.bin').read_bytes()
                    self.publish_result(output, images, source_time)
                    last_sequence = sequence
                    self.message = '보정 영상으로 Metric Depth 추론 중 · 모델 출력은 제어에 사용하지 않습니다.'
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.images = {}
            self.state = 'error'
            self.message = 'Depth 처리 실패: '+str(error)[:300]
        finally:
            await self._close_process()
