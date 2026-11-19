"""보정 영상과 실측 초점거리를 사용한 공식 Depth Pro 근거리 비교 실험."""
import argparse
import contextlib
import csv
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np
import torch

from depth.processing import Undistorter
from depth.roi import summarize_region


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--measurements',default='data/depth_evaluation/measurements_v3.csv')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    source=Path(args.measurements)
    output=Path(args.output)
    output.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(root/'.depth-model/ml-depth-pro/src'))
    with contextlib.redirect_stdout(sys.stderr):
        from depth_pro.depth_pro import create_model_and_transforms,DEFAULT_MONODEPTH_CONFIG_DICT
        config=replace(DEFAULT_MONODEPTH_CONFIG_DICT,checkpoint_uri=str(root/'.depth-model/checkpoints/depth_pro.pt'))
        torch.set_num_threads(4)
        model,transform=create_model_and_transforms(config=config,device=torch.device('cuda'),precision=torch.float16)
        model.eval()
    undistorter=Undistorter(root/'config/camera_calibration.json')
    with source.open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    results=[]
    for row in rows:
        raw=cv2.imread(str(source.parent/(row['sample_id']+'_raw.jpg')))
        corrected=undistorter.apply(raw)
        for variant in ['known_focal','known_focal_square_pixels','estimated_focal']:
            image=corrected
            if variant=='known_focal_square_pixels':
                width=round(300*undistorter.new_K[1,1]/undistorter.new_K[0,0])
                image=cv2.resize(corrected,(width,300))
            actual_focal=undistorter.new_K[0,0]*image.shape[1]/300
            rgb=cv2.cvtColor(image,cv2.COLOR_BGR2RGB)
            tensor=transform(rgb)
            focal=None if variant=='estimated_focal' else torch.tensor(actual_focal,device='cuda')
            started=time.monotonic()
            with torch.inference_mode():
                prediction=model.infer(tensor,f_px=focal)
            depth=prediction['depth'].float().cpu().numpy()
            if depth.shape!=(300,300):
                depth=cv2.resize(depth,(300,300),interpolation=cv2.INTER_LINEAR)
            roi=json.loads(row['roi'])
            summary=summarize_region(depth.astype('<f4').tobytes(),undistorter.mask.astype('uint8').tobytes(),300,300,roi,undistorter.new_K.tolist())
            gt=float(row['ground_truth_m'])
            result={'variant':variant,'sample_id':row['sample_id'],'scene':row['scene'],'ground_truth_ray_m':gt,
                    'prediction_ray_m':summary['model_ray_range_m'],'error_m':summary['model_ray_range_m']-gt,
                    'focal_px_used':float(prediction['focallength_px'].item()),'processing_s':time.monotonic()-started,
                    'passes_1cm_on_this_sample':abs(summary['model_ray_range_m']-gt)<=.01}
            results.append(result)
            np.save(output/(variant+'_'+row['sample_id']+'.npy'),depth)
            print(json.dumps(result,ensure_ascii=False),flush=True)
    reports=[]
    for variant in ['known_focal','known_focal_square_pixels','estimated_focal']:
        group=[r for r in results if r['variant']==variant]
        errors=[abs(r['error_m']) for r in group]
        reports.append({'variant':variant,'mae_m':sum(errors)/len(errors),'max_error_m':max(errors),
                        'passes_all_within_1cm':all(e<=.01 for e in errors)})
    report={'requirements':{'input':'undistorted_300x300','max_distance_m':1,'max_absolute_error_m':.01},
            'samples':results,'summary':reports,'validated_for_control':False}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
