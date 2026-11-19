import json
import unittest
from urllib.request import urlopen

from robot.camera_bridge import start_bridge


class Value:
    def copy(self):
        return self


class ExistingCamera:
    value = Value()


class CameraBridgeTest(unittest.TestCase):
    def test_existing_camera_forwarding_and_cleanup(self):
        camera = ExistingCamera()
        value = camera.value
        jpeg = b'\xff\xd8test\xff\xd9'
        bridge = start_bridge(camera, lambda image: jpeg, host='127.0.0.1')
        try:
            bridge.worker.join(timeout=.05)
            with urlopen('http://127.0.0.1:{}/health'.format(bridge.port), timeout=2) as response:
                self.assertFalse(json.load(response)['camera_owned_by_bridge'])
            with urlopen('http://127.0.0.1:{}/frame.jpg'.format(bridge.port), timeout=2) as response:
                self.assertEqual(response.read(), jpeg)
                self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
            self.assertIs(camera.value, value)
        finally:
            bridge.stop()
        self.assertFalse(bridge.worker.is_alive())
        self.assertFalse(bridge.web.is_alive())

    def test_invalid_encoder_fails_without_camera_access(self):
        with self.assertRaises(ValueError):
            start_bridge(None, None, host='127.0.0.1')
