import asyncio
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.camera import CameraConfig, CameraService, JpegFrames, MAX_FRAME_BYTES
from backend import camera_api, main

JPEG = b'\xff\xd8test-jpeg\xff\xd9'


def multipart(frame=JPEG, length=True):
    return b'--frame\r\nContent-Type: image/jpeg\r\n' + (
        b'Content-Length: ' + str(len(frame)).encode() + b'\r\n' if length else b'') + b'\r\n'+frame+b'\r\n--frame--\r\n'


class CameraParserTest(unittest.TestCase):
    def test_config_requires_full_url_and_format(self):
        for url, fmt in [('', ''), ('rtsp://camera/live', 'mjpeg'), ('http://camera/live', ''),
                         ('http://user:password@camera/live', 'mjpeg'), ('http://camera:bad/live', 'mjpeg')]:
            self.assertIsNotNone(CameraConfig(url, fmt).error())
        self.assertIsNone(CameraConfig('http://camera/confirmed-path', 'mjpeg').error())

    def test_split_multipart_with_and_without_lengths(self):
        for length in (False, True):
            parser = JpegFrames(b'frame')
            frames = []
            for byte in multipart(length=length):
                frames.extend(parser.feed(bytes([byte])))
            self.assertEqual(frames, [JPEG])

    def test_jpeg_embedded_thumbnail_does_not_split_frame(self):
        jpeg = b'\xff\xd8header\xff\xd8thumbnail\xff\xd9body\xff\xd9'
        self.assertEqual(JpegFrames(b'frame').feed(multipart(jpeg)), [jpeg])

    def test_oversized_and_invalid_parts(self):
        with self.assertRaises(ValueError):
            JpegFrames(b'frame').feed(b'--frame\r\nContent-Length: '+str(MAX_FRAME_BYTES+1).encode()+b'\r\n\r\n')
        with self.assertRaises(ValueError):
            JpegFrames(b'frame').feed(multipart(b'not-a-jpeg'))


class CameraReceiverTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_and_disconnect(self):
        def handler(request):
            return httpx.Response(200, content=JPEG, headers={'Content-Type':'image/jpeg'})
        service=CameraService(CameraConfig('http://camera/frame','snapshot'), httpx.MockTransport(handler))
        await service.start()
        for _ in range(20):
            if service.frame:
                break
            await asyncio.sleep(.01)
        self.assertTrue(service.snapshot()['frame_available'])
        self.assertEqual(service.frame, JPEG)
        await service.stop()
        self.assertFalse(service.snapshot()['frame_available'])
        self.assertIsNone(service.task)

    async def test_html_and_redirect_are_rejected(self):
        for response in [httpx.Response(200, content=b'web page',headers={'Content-Type':'text/html'}),
                         httpx.Response(302,headers={'Location':'http://other-camera/live'})]:
            seen=[]
            def handler(request):
                seen.append(str(request.url))
                return response
            service=CameraService(CameraConfig('http://camera/live','mjpeg'))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as client:
                with self.assertRaises(ValueError):
                    await service._read_once(client)
            self.assertIsNone(service.frame)
            self.assertEqual(len(seen),1)

    async def test_stale_frame_is_unavailable(self):
        service=CameraService()
        service._publish(JPEG)
        service.last_frame_monotonic = time.monotonic()-6
        self.assertFalse(service.snapshot()['frame_available'])

    async def test_broken_snapshot_does_not_reuse_old_frame(self):
        service=CameraService(CameraConfig('http://camera/frame','snapshot'))
        service._publish(JPEG)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'',headers={'Content-Type':'image/jpeg'}))) as client:
            with self.assertRaises(ValueError):
                await service._read_once(client)
        self.assertEqual(service.sequence,1)


class CameraApiTest(unittest.TestCase):
    def setUp(self):
        self.camera=CameraService(CameraConfig())
        self.enterContext(patch.object(camera_api,'camera',self.camera))
        self.client=self.enterContext(TestClient(main.app))

    def test_unconfigured_and_page(self):
        self.assertEqual(self.client.get('/video').status_code,200)
        for path in ['/static/video.js','/static/video.css']:
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertFalse(self.client.get('/api/camera/status').json()['configured'])
        self.assertEqual(self.client.post('/api/camera/connect').status_code,409)
        self.assertEqual(self.client.get('/api/camera/frame').status_code,503)

    def test_frame_headers_and_disconnect(self):
        self.camera._publish(JPEG)
        result=self.client.get('/api/camera/frame')
        self.assertEqual(result.content,JPEG)
        self.assertEqual(result.headers['cache-control'],'no-store')
        self.assertEqual(result.headers['content-type'],'image/jpeg')
        self.assertEqual(result.headers['x-frame-sequence'],'1')
        self.assertFalse(self.client.post('/api/camera/disconnect').json()['frame_available'])

    def test_secondary_video_has_independent_frames_and_disconnect(self):
        secondary = CameraService(CameraConfig('http://camera-b/frame', 'snapshot'))
        self.enterContext(patch.object(camera_api, 'secondary_camera', secondary))
        secondary._publish(JPEG)
        self.assertEqual(self.client.get('/api/camera/secondary/frame').content, JPEG)
        self.assertTrue(self.client.get('/api/camera/secondary/status').json()['frame_available'])
        self.assertEqual(self.client.get('/api/camera/frame').status_code, 503)
        self.assertFalse(self.client.post('/api/camera/secondary/disconnect').json()['frame_available'])


if __name__=='__main__':
    unittest.main()
