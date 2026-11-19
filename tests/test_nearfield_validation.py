import unittest

from depth.nearfield_validation import validate_nearfield


class NearfieldValidationTest(unittest.TestCase):
    def samples(self,errors):
        return [dict(sample_id=str(index),ground_truth_ray_m=.5,prediction_ray_m=.5+error) for index,error in enumerate(errors)]

    def test_small_mean_does_not_hide_failed_sample(self):
        report=validate_nearfield(self.samples([0,0,0,.02]))
        self.assertLess(report['mae_m'],.01)
        self.assertFalse(report['test_set_passed'])
        self.assertEqual(report['passed_samples'],3)

    def test_boundary_passes_without_claiming_full_range(self):
        report=validate_nearfield(self.samples([.01,-.01]))
        self.assertTrue(report['test_set_passed'])
        self.assertFalse(report['full_range_verified'])
        self.assertFalse(report['usable_for_grasp'])

    def test_invalid_inputs_do_not_pass(self):
        for samples in [[],[dict(sample_id='x',ground_truth_ray_m=1.1,prediction_ray_m=1.1)],self.samples([float('nan')]),self.samples([0])+self.samples([0])]:
            with self.assertRaises(ValueError):
                validate_nearfield(samples)
