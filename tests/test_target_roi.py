import struct
import unittest

from depth.roi import summarize_region


class TargetRoiTest(unittest.TestCase):
    def test_selected_object_is_not_background_median(self):
        values=[3.0]*100
        for row in range(2,6):
            for col in range(2,6):
                values[row*10+col]=.5
        result=summarize_region(struct.pack('<100f',*values),bytes([1]*100),10,10,[2,2,4,4])
        self.assertAlmostEqual(result['model_target_m'],.5)
        self.assertFalse(result['wall_calibration_applied'])
        self.assertFalse(result['usable_for_grasp'])

    def test_invalid_and_masked_values(self):
        result=summarize_region(struct.pack('<100f',*([float('nan')]*100)),bytes([1]*100),10,10,[0,0,4,4])
        self.assertIsNone(result['model_target_m'])
        result=summarize_region(struct.pack('<100f',*([1]*100)),bytes(100),10,10,[0,0,4,4])
        self.assertIsNone(result['model_target_m'])

    def test_bad_bounds(self):
        for roi in [[9,9,4,4],[-1,0,4,4],[0,0,1,1]]:
            with self.assertRaises(ValueError):
                summarize_region(bytes(400),bytes(100),10,10,roi)

    def test_large_depth_spread_is_flagged(self):
        result=summarize_region(struct.pack('<100f',*([1]*50+[3]*50)),bytes([1]*100),10,10,[0,0,10,10])
        self.assertTrue(result['mixed_depth_warning'])

    def test_ray_range_uses_corrected_intrinsics(self):
        matrix=[[10,0,0],[0,10,0],[0,0,1]]
        result=summarize_region(struct.pack('<100f',*([2]*100)),bytes([1]*100),10,10,[5,5,4,4],matrix)
        self.assertEqual(result['model_depth_z_m'],2)
        self.assertGreater(result['model_ray_range_m'],2.4)
        self.assertLess(result['model_ray_range_m'],3)
        with self.assertRaises(ValueError):
            summarize_region(bytes(400),bytes(100),10,10,[0,0,4,4],[[0,0,0],[0,10,0],[0,0,1]])
