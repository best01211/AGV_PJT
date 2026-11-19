import tempfile
import csv
from pathlib import Path
import time
import struct
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient

from backend import depth_api, main
from backend.depth_service import DepthService
from depth.evaluate import evaluate


class DepthApiTest(unittest.TestCase):
    def setUp(self):
        self.service=DepthService()
        self.enterContext(patch.object(depth_api,'depth_service',self.service))
        self.client=self.enterContext(TestClient(main.app))

    def test_no_stale_output_or_arbitrary_file_access(self):
        self.assertFalse(self.client.get('/api/depth/status').json()['result_available'])
        self.assertEqual(self.client.get('/api/depth/frame/depth').status_code,503)
        self.assertEqual(self.client.get('/api/depth/frame/other').status_code,404)
        self.assertEqual(self.client.post('/api/depth/measurements',json={'scene':'board','ground_truth_m':1,'sequence':1,'run_id':1}).status_code,409)
        self.assertEqual(self.client.post('/api/depth/measurements',json={'scene':'board','ground_truth_m':-1,'sequence':1,'run_id':1}).status_code,422)

    def test_missing_runtime_rejected(self):
        with patch.object(self.service,'paths',return_value=(__import__('pathlib').Path('missing/python'),__import__('pathlib').Path('missing/repo'),__import__('pathlib').Path('missing/model'))):
            self.assertEqual(self.client.post('/api/depth/start').status_code,409)

    def publish(self, sequence, prediction=1.5, age=0):
        self.service.state='running'
        self.service.run_id=1
        images={kind:(kind+str(sequence)).encode() for kind in ['array','raw','undistorted','depth']}
        self.service.publish_result({'sequence':sequence,'summary':{'model_center_m':prediction}},images,time.monotonic()-age)

    def test_record_displayed_frame_after_new_inference(self):
        self.publish(10,1.5)
        self.publish(13,9.0)
        with tempfile.TemporaryDirectory() as directory, patch.object(depth_api,'MEASUREMENT_DIR',Path(directory)):
            response=self.client.post('/api/depth/measurements',json={'scene':'test fixture','ground_truth_m':.3,'sequence':10,'run_id':1})
            self.assertEqual(response.status_code,200)
            saved=response.json()
            self.assertEqual(saved['prediction_m'],1.5)
            self.assertEqual((Path(directory)/(saved['sample_id']+'.npz')).read_bytes(),b'array10')
            self.assertEqual((Path(directory)/(saved['sample_id']+'_raw.jpg')).read_bytes(),b'raw10')
            with (Path(directory)/'measurements.csv').open(encoding='utf-8',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(rows[0]['sequence'],'10')

    def test_versioned_images_do_not_switch_to_latest(self):
        self.publish(10)
        self.publish(13)
        response=self.client.get('/api/depth/frame/depth?run_id=1&sequence=10')
        self.assertEqual(response.content,b'depth10')
        self.assertEqual(response.headers['x-depth-sequence'],'10')
        self.assertEqual(self.client.get('/api/depth/frame/depth?run_id=1').status_code,422)

    def test_expired_or_previous_run_is_rejected(self):
        self.publish(10,age=16)
        body={'scene':'test','ground_truth_m':.3,'sequence':10,'run_id':1}
        self.assertEqual(self.client.post('/api/depth/measurements',json=body).status_code,409)
        self.publish(13)
        self.service.run_id=2
        self.assertEqual(self.client.get('/api/depth/frame/depth?run_id=1&sequence=13').status_code,410)

    def test_cache_is_bounded(self):
        for sequence in range(50):
            self.publish(sequence)
        self.assertEqual(len(self.service.recent_results),32)
        self.assertIsNone(self.service.get_result(1,0))

    def test_base_records_are_separated_from_legacy_small_csv(self):
        self.publish(10,1.5)
        entry=self.service.get_result(1,10)
        entry['result'].update(encoder='vitb',input_size=294,profile_id='fixture')
        entry['result']['summary']['distance_correction']={'corrected_center_m':.4}
        with tempfile.TemporaryDirectory() as directory, patch.object(depth_api,'MEASUREMENT_DIR',Path(directory)):
            response=self.client.post('/api/depth/measurements',json={'scene':'fixture','ground_truth_m':.4,'sequence':10,'run_id':1})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['encoder'],'vitb')
            self.assertTrue((Path(directory)/'measurements_v2.csv').is_file())
            self.assertFalse((Path(directory)/'measurements.csv').exists())

    def test_pinned_target_survives_live_updates_and_records_roi(self):
        self.publish(10)
        entry=self.service.get_result(1,10)
        entry['result'].update(encoder='vitb',input_size=294,camera_matrix=[[100,0,150],[0,100,150],[0,0,1]])
        entry['images'].update(depth_values=struct.pack('<90000f',*([.75]*90000)),valid_values=bytes([1]*90000))
        token=self.client.post('/api/depth/pin',json={'run_id':1,'sequence':10}).json()['token']
        for sequence in range(11,60):
            self.publish(sequence)
        self.assertIsNone(self.service.get_result(1,10))
        payload={'token':token,'x':10,'y':10,'width':20,'height':20}
        result=self.client.post('/api/depth/probe',json=payload).json()
        self.assertEqual(result['sequence'],10)
        self.assertAlmostEqual(result['model_target_m'],.75)
        self.assertFalse(result['wall_calibration_applied'])
        with tempfile.TemporaryDirectory() as directory, patch.object(depth_api,'MEASUREMENT_DIR',Path(directory)):
            saved=self.client.post('/api/depth/target-measurements',json=dict(payload,scene='target fixture',ground_truth_m=.5))
            self.assertEqual(saved.status_code,200)
            self.assertEqual(saved.json()['measurement_kind'],'selected_target')
            self.assertTrue((Path(directory)/'measurements_v4.csv').exists())
            self.assertEqual(saved.json()['distance_kind'],'camera_ray_range')
            self.assertGreater(saved.json()['prediction_ray_range_m'],saved.json()['prediction_depth_z_m'])
            self.assertEqual((Path(directory)/(saved.json()['sample_id']+'_raw.jpg')).read_bytes(),b'raw10')
        self.service.pinned_results[token]['pinned_at']-=121
        self.assertEqual(self.client.post('/api/depth/probe',json=payload).status_code,410)


class DepthEvaluationTest(unittest.TestCase):
    def test_pdf_group_means_expose_order_inversion(self):
        import json
        from pathlib import Path
        groups=json.loads((Path(__file__).resolve().parents[1]/'config/depth_reference_evaluation.json').read_text())['groups']
        report=evaluate(groups)
        self.assertEqual(report['sample_count'],4)
        self.assertAlmostEqual(report['mae_m'],.32)
        self.assertEqual(report['distance_order_inversions'],1)
        self.assertFalse(report['usable_for_control'])

    def test_invalid_measurements_rejected(self):
        for value in [-1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):
                evaluate([{'ground_truth_m':1,'prediction_m':value}])
