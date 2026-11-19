"""설정된 카메라 URL만 읽는 영상 수신기. 로봇 제어와 독립적입니다."""
import asyncio
import os
import time
from email.message import Message
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

MAX_FRAME_BYTES = 5 * 1024 * 1024
STALE_SECONDS = 5


@dataclass(frozen=True)
class CameraConfig:
    url: str = ''
    format: str = ''

    @classmethod
    def from_environment(cls):
        return cls(os.getenv('JETANK_CAMERA_URL', '').strip(),
                   os.getenv('JETANK_CAMERA_FORMAT', '').strip().lower())

    def error(self):
        if not self.url:
            return 'JETANK_CAMERA_URL을 설정해 주세요.'
        try:
            parsed = urlsplit(self.url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.netloc.endswith(':') or parsed.port == 0:
                return '카메라 주소는 유효한 HTTP(S) URL이어야 합니다.'
            if parsed.username or parsed.password or parsed.fragment:
                return '인증정보·fragment가 포함되지 않은 영상 URL을 사용해 주세요.'
        except ValueError:
            return '카메라 URL 형식이 올바르지 않습니다.'
        if self.format not in ('mjpeg', 'snapshot'):
            return 'JETANK_CAMERA_FORMAT을 mjpeg 또는 snapshot으로 설정해 주세요.'
        return None


class JpegFrames:
    """Multipart 경계/Content-Length로 분할. JPEG 내부 EXIF의 종료 마커를 경계로 오인하지 않음."""
    def __init__(self, boundary):
        if not boundary or len(boundary) > 200 or any(v < 33 or v > 126 for v in boundary):
            raise ValueError('content_type')
        self.delimiter = b'--' + boundary
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(self.delimiter)
            if start < 0:
                if len(self.buffer) > 16384:
                    raise ValueError('no_frames')
                break
            if start:
                del self.buffer[:start]
            header_end = self.buffer.find(b'\r\n\r\n')
            if header_end < 0:
                if len(self.buffer) > 16384:
                    raise ValueError('content_type')
                break
            if header_end > 16384:
                raise ValueError('content_type')
            header = self.buffer[len(self.delimiter):header_end].decode('latin-1')
            lengths = [line.split(':', 1)[1].strip() for line in header.split('\r\n') if line.lower().startswith('content-length:')]
            body_start = header_end + 4
            if lengths:
                size = int(lengths[0])
                if not 4 <= size <= MAX_FRAME_BYTES:
                    raise ValueError('frame_too_large')
                end = body_start + size
                if len(self.buffer) < end:
                    break
            else:
                end = self.buffer.find(b'\r\n' + self.delimiter, body_start)
                if end < 0:
                    if len(self.buffer) - body_start > MAX_FRAME_BYTES:
                        raise ValueError('frame_too_large')
                    break
            frame = bytes(self.buffer[body_start:end])
            if len(frame) > MAX_FRAME_BYTES:
                raise ValueError('frame_too_large')
            if not frame.startswith(b'\xff\xd8') or not frame.endswith(b'\xff\xd9'):
                raise ValueError('invalid_jpeg')
            frames.append(frame)
            del self.buffer[:end]
        return frames


class CameraService:
    def __init__(self, config=None, transport=None):
        self.config = config or CameraConfig.from_environment()
        self.transport = transport
        self.task = None
        self.frame = None
        self.sequence = 0
        self.capture_id = None
        self.last_frame_at = None
        self.last_frame_monotonic = None
        self.state = 'disconnected'
        self.message = '영상 연결을 시작해 주세요.'

    def snapshot(self):
        age = None if self.last_frame_monotonic is None else time.monotonic() - self.last_frame_monotonic
        return {'configured': self.config.error() is None,
                'configuration_error': self.config.error(),
                'format': self.config.format or None, 'state': self.state,
                'message': self.message, 'sequence': self.sequence,
                'last_frame_at': self.last_frame_at,
                'frame_age_s': None if age is None else round(age, 2),
                'frame_available': self.frame is not None and age is not None and age <= STALE_SECONDS,
                'motor_mode': 'mock', 'robot_control_connected': False}

    async def start(self):
        error = self.config.error()
        if error:
            raise ValueError(error)
        if self.task is not None and not self.task.done():
            return self.snapshot()
        self.state, self.message = 'connecting', '카메라에 연결 중입니다.'
        self.task = asyncio.create_task(self._receive())
        return self.snapshot()

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self.frame = None
        self.last_frame_at = self.last_frame_monotonic = None
        self.capture_id = None
        self.state, self.message = 'disconnected', '영상 연결을 종료했습니다.'
        return self.snapshot()

    def _publish(self, frame, capture_id=None):
        if capture_id is not None and capture_id == self.capture_id:
            return
        self.capture_id = capture_id
        self.frame = frame
        self.sequence += 1
        self.last_frame_at = time.time()
        self.last_frame_monotonic = time.monotonic()
        self.state, self.message = 'receiving', '카메라 프레임 수신 중입니다.'

    async def _read_once(self, client):
        async with client.stream('GET', self.config.url, headers={'Accept': 'multipart/x-mixed-replace,image/jpeg'}) as response:
            if response.status_code != 200:
                raise ValueError('upstream_http')
            content_type = response.headers.get('content-type', '').lower().split(';')[0].strip()
            expected = 'multipart/x-mixed-replace' if self.config.format == 'mjpeg' else 'image/jpeg'
            if content_type != expected:
                raise ValueError('content_type')
            mime = Message()
            mime['content-type'] = response.headers['content-type']
            boundary = mime.get_param('boundary')
            if boundary is not None and not isinstance(boundary, str):
                raise ValueError('content_type')
            parser = JpegFrames(boundary.encode('ascii')) if self.config.format == 'mjpeg' and boundary else None
            if self.config.format == 'mjpeg' and parser is None:
                raise ValueError('content_type')
            total_bytes = 0
            snapshot = bytearray()
            last_valid_frame = time.monotonic()
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                if self.config.format == 'snapshot':
                    total_bytes += len(chunk)
                    if total_bytes > MAX_FRAME_BYTES:
                        raise ValueError('frame_too_large')
                    snapshot.extend(chunk)
                frames = parser.feed(chunk) if parser else []
                for frame in frames:
                    self._publish(frame)
                    last_valid_frame = time.monotonic()
                if time.monotonic() - last_valid_frame > STALE_SECONDS:
                    raise ValueError('no_frames')
            if self.config.format == 'snapshot':
                if not snapshot.startswith(b'\xff\xd8') or not snapshot.endswith(b'\xff\xd9'):
                    raise ValueError('invalid_jpeg')
                capture_id = response.headers.get('X-Capture-Id')
                self._publish(bytes(snapshot), capture_id if capture_id and len(capture_id) <= 64 else None)
            else:
                raise ValueError('stream_ended')

    async def _receive(self):
        delay = 1
        errors = {'upstream_http': '영상 서버 응답 오류입니다. 주소·포트·인증을 확인해 주세요.',
                  'content_type': '영상 형식이 설정과 다릅니다. MJPEG/JPEG 주소인지 확인해 주세요.',
                  'frame_too_large': '영상 프레임이 허용 크기(5 MB)를 초과했습니다.',
                  'no_frames': '영상 데이터에서 JPEG 프레임을 찾지 못했습니다.',
                  'invalid_jpeg': '유효한 JPEG 프레임이 아닙니다. 영상 주소와 형식을 확인해 주세요.',
                  'stream_ended': '영상 스트림이 종료됐습니다.'}
        async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(5),
                                    follow_redirects=False, trust_env=False) as client:
            while True:
                try:
                    await self._read_once(client)
                    delay = 1
                    await asyncio.sleep(.2)
                except (httpx.HTTPError, ValueError) as error:
                    self.state = 'reconnecting'
                    self.frame = None
                    self.message = errors.get(str(error), '카메라에 연결할 수 없습니다. 네트워크와 영상 서버를 확인해 주세요.')
                    # URL/인증정보가 포함될 수 있는 예외 원문은 응답에 노출하지 않습니다.
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10)
