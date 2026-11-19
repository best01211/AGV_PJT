"""저장된 동일 RGB/실측 자료로 공식 Metric 모델을 비교. 보정식은 학습하지 않습니다."""
import argparse
import contextlib
import csv
import gc
import json
from pathlib import Path
import re
import sys
import time

import cv2
import numpy as np
import torch

from depth.evaluate import evaluate
from depth.processing import Undistorter, summarize_depth
from depth.measurements import load_measurements
from depth.roi import summarize_region


CONFIGS = {
    'vits': {'encoder':'vits','features':64,'out_channels':[48,96,192,384]},
    'vitb': {'encoder':'vitb','features':128,'out_channels':[96,192,384,768]},
    'vitl': {'encoder':'vitl','features':256,'out_channels':[256,512,1024,1024]},
}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--measurements',default='data/depth_evaluation/measurements.csv')
    parser.add_argument('--output',required=True)
    parser.add_argument('--encoders',nargs='+',choices=list(CONFIGS),default=list(CONFIGS))
    parser.add_argument('--input-sizes',nargs='+',type=int,default=[294,518])
    parser.add_argument('--assume-distance-kind',choices=['unspecified','optical_z','camera_ray_range'],default='unspecified',help='Only for older records whose measurement basis has been explicitly confirmed')
    args=parser.parse_args()
    if any(size<140 or size>1022 or size%14 for size in args.input_sizes):
        parser.error('input sizes must be multiples of 14 from 140 to 1022')
    root=Path(__file__).resolve().parents[1]
    measurements=Path(args.measurements).resolve()
    output=Path(args.output)
    output.mkdir(parents=True,exist_ok=True)
    rows=load_measurements(measurements)
    if not rows:
        raise ValueError('No measurements')
    undistorter=Undistorter(root/'config/camera_calibration.json')
    samples=[]
    for row in rows:
        if not re.fullmatch('[a-f0-9]{32}',row['sample_id']):
            raise ValueError('Invalid sample ID')
        raw=cv2.imread(str(measurements.parent/(row['sample_id']+'_raw.jpg')))
        samples.append((row,undistorter.apply(raw)))
    sys.path.insert(0,str(root/'.depth-model/Depth-Anything-V2/metric_depth'))
    with contextlib.redirect_stdout(sys.stderr):
        from depth_anything_v2.dpt import DepthAnythingV2
    device='cuda' if torch.cuda.is_available() else 'cpu'
    torch.set_num_threads(4)
    results=[]
    summaries=[]
    for encoder in args.encoders:
        checkpoint=root/'.depth-model/checkpoints'/('depth_anything_v2_metric_hypersim_'+encoder+'.pth')
        with contextlib.redirect_stdout(sys.stderr):
            model=DepthAnythingV2(**CONFIGS[encoder],max_depth=20)
            model.load_state_dict(torch.load(checkpoint,map_location='cpu',weights_only=True))
            model=model.to(device).eval()
        for size in args.input_sizes:
            comparison=[]
            model_dir=output/(encoder+'_'+str(size))
            model_dir.mkdir(exist_ok=True)
            for row,image in samples:
                if device=='cuda':
                    torch.cuda.synchronize()
                started=time.monotonic()
                with contextlib.redirect_stdout(sys.stderr),torch.inference_mode():
                    depth=model.infer_image(image,input_size=size)
                if device=='cuda':
                    torch.cuda.synchronize()
                elapsed=(time.monotonic()-started)*1000
                roi=json.loads(row['roi']) if row.get('roi') else None
                distance_kind=row.get('distance_kind') or args.assume_distance_kind
                if roi or distance_kind=='camera_ray_range':
                    region=summarize_region(depth.astype('<f4').tobytes(),undistorter.mask.astype(np.uint8).tobytes(),300,300,roi or [140,140,20,20],undistorter.new_K.tolist())
                    prediction=region['model_ray_range_m'] if distance_kind=='camera_ray_range' else region['model_depth_z_m']
                else:
                    prediction=summarize_depth(depth,undistorter.mask)['model_center_m']
                if prediction is None:
                    raise ValueError('Insufficient center pixels: '+row['sample_id'])
                gt=float(row['ground_truth_m'])
                item={'encoder':encoder,'input_size':size,'sample_id':row['sample_id'],
                      'scene':row['scene'],'ground_truth_m':gt,'prediction_m':prediction,
                      'error_m':prediction-gt,'inference_ms':round(elapsed,2),
                      'roi':json.dumps(roi or [140,140,20,20]),
                      'distance_kind':distance_kind,
                      'measurement_kind':row.get('measurement_kind') or 'center'}
                comparison.append(item)
                results.append(item)
                np.save(model_dir/(row['sample_id']+'.npy'),depth)
            report=dict(encoder=encoder,input_size=size,**evaluate(comparison))
            summaries.append(report)
            print(json.dumps(report),flush=True)
        del model
        gc.collect()
        if device=='cuda':
            torch.cuda.empty_cache()
    with (output/'predictions.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    report={'device':device,'source_measurements':str(measurements),'sample_count':len(samples),
            'calibration':undistorter.config,'summary':summaries,
            'warning':'Same small dataset used for comparison; no held-out validation. No model certified for control.'}
    (output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
