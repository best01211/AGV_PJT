"""Python 3.6+ Jupyter bridge: reuse an existing camera object, never create motors/cameras."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class CameraBridge:
    def __init__(self, camera, encoder, host='0.0.0.0', port=0, fps=5):
        if not callable(encoder):
            raise ValueError('JPEG encoder is required')
        if not 1 <= fps <= 15:
            raise ValueError('fps must be between 1 and 15')
        self.camera = camera
        self.encoder = encoder
        self.frame = None
        self.updated = None
        self.error = None
        self.last_value = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/health':
                    body = json.dumps(bridge.status()).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                elif self.path == '/frame.jpg':
                    with bridge.lock:
                        body = bridge.frame
                        age = None if bridge.updated is None else time.monotonic() - bridge.updated
                    if body is None or age is None or age > 3:
                        self.send_response(503)
                        body = b'No recent camera frame'
                        self.send_header('Content-Type', 'text/plain')
                    else:
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/jpeg')
                else:
                    self.send_response(404)
                    body = b'Not found'
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]
        self.interval = 1.0 / fps
        self.worker = threading.Thread(target=self._capture, daemon=True)
        self.web = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.web.start()

    def _capture(self):
        while not self.stopped.is_set():
            try:
                # Read only the camera's published value. No read(), running=True or device reinitialization.
                value = self.camera.value
                if value is None:
                    raise ValueError('Existing camera has no frame')
                if value is self.last_value:
                    self.stopped.wait(self.interval)
                    continue
                image = value.copy()
                frame = bytes(self.encoder(image))
                if not frame.startswith(b'\xff\xd8') or not frame.endswith(b'\xff\xd9') or len(frame) > 5 * 1024 * 1024:
                    raise ValueError('Encoder did not return a supported JPEG frame')
                with self.lock:
                    self.frame = frame
                    self.last_value = value
                    self.updated = time.monotonic()
                    self.error = None
            except Exception as error:
                with self.lock:
                    self.frame = None
                    self.updated = None
                    self.error = type(error).__name__
            self.stopped.wait(self.interval)

    def status(self):
        with self.lock:
            age = None if self.updated is None else time.monotonic() - self.updated
            return {'frame_available': self.frame is not None and age is not None and age <= 3,
                    'bridge_port': self.port, 'error': self.error,
                    'camera_owned_by_bridge': False,
                    'note': 'Bridge receipt time is not the camera capture timestamp.'}

    def stop(self):
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.web.join(timeout=2)
        self.worker.join(timeout=2)


def start_bridge(camera, encoder, host='0.0.0.0', port=0, fps=5):
    return CameraBridge(camera, encoder, host=host, port=port, fps=fps)

# Run this cell in the SAME kernel as the existing camera notebook.
if 'camera' not in globals() or 'bgr8_to_jpeg' not in globals():
    raise RuntimeError('기존 영상이 실행 중인 노트북의 같은 커널에서 실행해 주세요. camera와 bgr8_to_jpeg가 필요합니다.')
if 'rove_camera_bridge' in globals():
    rove_camera_bridge.stop()
rove_camera_bridge = start_bridge(camera, bgr8_to_jpeg)
print('Desktop camera URL: http://70.12.231.81:{}/frame.jpg'.format(rove_camera_bridge.port))
print('Desktop format: snapshot')
print('Stop bridge: rove_camera_bridge.stop()')
