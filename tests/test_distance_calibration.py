import unittest

from depth.calibrate_distance import fit,build_profile
from depth.validate_distance import validate
from depth.distance_correction import correct_center


class DistanceCalibrationTest(unittest.TestCase):
    def rows(self,predictions):
        return [dict(encoder='vitb',input_size='294',scene='fixture',sample_id=str(i),prediction_m=str(p),ground_truth_m=str(g)) for i,(p,g) in enumerate(zip(predictions,[.3,.5,.8,1.2]))]

    def test_known_affine_and_held_out_diagnostics(self):
        profile=build_profile(self.rows([1.6,2,2.6,3.4]),'vitb',294)
        self.assertAlmostEqual(profile['scale'],.5)
        self.assertAlmostEqual(profile['offset_m'],-.5)
        self.assertAlmostEqual(profile['leave_one_out_diagnostic']['mae_m'],0)
        self.assertFalse(profile['enabled'])
        self.assertFalse(profile['apply_to_dense_depth'])

    def test_reversed_order_is_rejected(self):
        with self.assertRaises(ValueError):
            build_profile(self.rows([1,3,2,4]),'vitb',294)

    def test_degenerate_or_insufficient_data_rejected(self):
        for points in [[(1,.3),(1,.5),(1,.8)],[(1,.3),(2,.5)],[(float('nan'),.3),(2,.5),(3,.8)]]:
            with self.assertRaises(ValueError):
                fit(points)

    def test_fixed_profile_uses_only_held_out_observations(self):
        training=self.rows([1.6,2,2.6,3.4])
        profile=build_profile(training,'vitb',294)
        held=dict(training[0],sample_id='new',ground_truth_m='.7',prediction_m='2.4')
        report=validate(profile,training+[held])
        self.assertEqual(report['sample_count'],1)
        self.assertAlmostEqual(report['mae_m'],0)
        with self.assertRaises(ValueError):
            validate(profile,training)

    def test_profile_is_not_extrapolated(self):
        training=self.rows([1.6,2,2.6,3.4])
        profile=build_profile(training,'vitb',294)
        with self.assertRaises(ValueError):
            validate(profile,[dict(training[0],sample_id='new',prediction_m='10')])

    def test_runtime_correction_requires_matching_model_and_domain(self):
        profile=build_profile(self.rows([1.6,2,2.6,3.4]),'vitb',294)
        self.assertEqual(correct_center(2.4,profile,'vitb',294)['reason'],'profile_disabled')
        profile['enabled']=True
        result=correct_center(2.4,profile,'vitb',294)
        self.assertAlmostEqual(result['corrected_center_m'],.7)
        self.assertFalse(result['usable_for_grasp'])
        for value in [None,float('nan'),.2,10]:
            self.assertIsNone(correct_center(value,profile,'vitb',294)['corrected_center_m'])
        self.assertEqual(correct_center(2.4,profile,'vits',294)['reason'],'model_mismatch')
