import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from depth.processing import Undistorter, summarize_depth, colorize_depth


CALIBRATION = Path(__file__).resolve().parents[2]/'config/camera_calibration.json'


class DepthProcessingTest(unittest.TestCase):
    def test_exact_resolution_required(self):
        undistorter=Undistorter(CALIBRATION)
        corrected=undistorter.apply(np.zeros((300,300,3),np.uint8))
        self.assertEqual(corrected.shape,(300,300,3))
        for shape in [(240,320,3),(600,600,3)]:
            with self.assertRaises(ValueError):
                undistorter.apply(np.zeros(shape,np.uint8))

    def test_invalid_calibration_rejected(self):
        data=json.loads(CALIBRATION.read_text())
        data['camera_matrix'][0][0]=-1
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'invalid.json'
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Undistorter(path)

    def test_center_median_and_no_scalar_correction(self):
        depth=np.full((300,300),3.2053,dtype=np.float32)
        mask=np.ones((300,300),bool)
        result=summarize_depth(depth,mask)
        self.assertAlmostEqual(result['model_center_m'],3.2053,places=4)
        self.assertFalse(result['usable_for_control'])
        depth[140:160,140:151]=np.nan
        self.assertIsNone(summarize_depth(depth,mask)['model_center_m'])

    def test_invalid_depth_pixels_gray(self):
        depth=np.full((300,300),2,dtype=np.float32)
        depth[0,0]=np.nan
        depth[0,1]=0
        depth[0,2]=20
        color=colorize_depth(depth,np.ones((300,300),bool))
        for col in range(3):
            np.testing.assert_array_equal(color[0,col],[96,96,96])
