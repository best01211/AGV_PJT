"""Serve the newest C270 JPEG to one authorized PC on the local network."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading
import time
from uuid import uuid4

import cv2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default='/dev/video1')
    parser.add_argument('--client', required=True)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    name = Path('/sys/class/video4linux') / Path(args.device).name / 'name'
    if 'C270' not in name.read_text().upper():
        parser.error('The selected video device is not a C270')

    latest = None
    stream_id = uuid4().hex
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.connection.settimeout(2)
            if self.client_address[0] != args.client:
                self.send_error(403)
                return
            if self.path != '/frame.jpg':
                self.send_error(404)
                return
            frame = latest
            if frame is None or time.monotonic() - frame[0] > 2:
                self.send_error(503, 'No recent frame')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(frame[1])))
            self.send_header('X-Capture-Id', frame[2])
            self.end_headers()
            try:
                self.wfile.write(frame[1])
            except OSError:
                pass

        def log_message(self, *args):
            pass

    with HTTPServer(('0.0.0.0', args.port), Handler) as server:
        camera = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
        if not camera.isOpened():
            camera.release()
            raise RuntimeError('Cannot open C270')
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        def capture():
            nonlocal latest
            next_jpeg = 0
            sequence = 0
            try:
                while not stopped.is_set():
                    ok, image = camera.read()
                    if not ok:
                        stopped.wait(.1)
                        continue
                    now = time.monotonic()
                    if now < next_jpeg:
                        continue
                    ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok:
                        sequence += 1
                        latest = (now, jpeg.tobytes(), '{}:{}'.format(stream_id, sequence))
                        next_jpeg = now + .1
            finally:
                camera.release()

        worker = threading.Thread(target=capture, daemon=True)
        worker.start()
        print('C270 snapshot server ready on port {}'.format(args.port), flush=True)
        try:
            server.serve_forever()
        finally:
            stopped.set()
            worker.join(3)


if __name__ == '__main__':
    main()
