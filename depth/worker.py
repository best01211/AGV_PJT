"""보정 영상 추론 GPU worker. 출력 수치/정확도 상태는 모델과 측정 기준을 구분합니다."""
import argparse
import contextlib
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np
import torch

from depth.processing import Undistorter,colorize_depth,summarize_depth
from depth.distance_correction import correct_center


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo',required=True)
    parser.add_argument('--checkpoint',required=True)
    parser.add_argument('--calibration',required=True)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--engine',choices=['depthpro','depthanything'],default='depthpro')
    parser.add_argument('--preview',action='store_true')
    args=parser.parse_args()
    calibration_path=Path(args.calibration)
    preview=args.preview and args.engine=='depthpro' and not calibration_path.is_file()
    fixed_focal=os.getenv('JETANK_C270_PREVIEW_FOCAL_PX') if preview else None
    estimated_focal=float(fixed_focal) if fixed_focal is not None else None
    if estimated_focal is not None and (not math.isfinite(estimated_focal) or estimated_focal<=0):
        raise ValueError('C270 preview focal length must be positive and finite')
    undistorter=None if preview else Undistorter(calibration_path)
    calibration_id='' if preview else hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    if preview:
        calibration_source='Fixed C270 preview focal estimate' if fixed_focal is not None else 'Depth Pro estimated focal length'
    else:
        calibration_source=undistorter.config['source']
    device='cuda' if torch.cuda.is_available() else 'cpu'
    with contextlib.redirect_stdout(sys.stderr):
        torch.set_num_threads(4)
        if args.engine=='depthpro':
            sys.path.insert(0,str(Path(args.repo)/'src'))
            from depth_pro.depth_pro import create_model_and_transforms,DEFAULT_MONODEPTH_CONFIG_DICT
            config=replace(DEFAULT_MONODEPTH_CONFIG_DICT,checkpoint_uri=args.checkpoint)
            model,transform=create_model_and_transforms(config=config,device=torch.device(device),precision=torch.float16 if device=='cuda' else torch.float32)
            model.eval()
            profile={'profile_id':'depthpro_estimated_intrinsics_v1' if preview else 'depthpro_calibrated_focal_'+calibration_id[:12],'extra_distance_fit':False,
                     'camera_intrinsics':None if preview else undistorter.new_K.tolist(),'metric_validated':False}
            encoder,input_size='depthpro',1536
        else:
            sys.path.insert(0,str(Path(args.repo)/'metric_depth'))
            from depth_anything_v2.dpt import DepthAnythingV2
            profile=json.loads(Path(args.profile).read_text(encoding='utf-8'))
            if profile['encoder']!='vitb' or profile['input_size']!=294:
                raise ValueError('Legacy profile must match Base 294')
            model=DepthAnythingV2(encoder='vitb',features=128,out_channels=[96,192,384,768],max_depth=20)
            model.load_state_dict(torch.load(args.checkpoint,map_location='cpu',weights_only=True))
            model=model.to(device).eval()
            encoder,input_size='vitb',294
    print(json.dumps({'ready':True,'device':device,'engine':args.engine}),flush=True)
    preview_size=(640,480) if fixed_focal is not None else None
    for line in sys.stdin:
        try:
            command=json.loads(line)
            started=time.monotonic()
            raw=cv2.imread(command['input'])
            if raw is None:
                raise ValueError('Cannot decode camera frame')
            corrected=raw if preview else undistorter.apply(raw)
            image_height,image_width=corrected.shape[:2]
            mask=np.ones((image_height,image_width),dtype=bool) if preview else undistorter.mask
            focal_used=None
            with contextlib.redirect_stdout(sys.stderr),torch.inference_mode():
                if args.engine=='depthpro':
                    if preview:
                        model_image=corrected
                        if preview_size is None:
                            preview_size=(image_width,image_height)
                        elif preview_size!=(image_width,image_height):
                            raise ValueError('C270 preview resolution changed; restart Depth')
                        focal_used=estimated_focal
                    else:
                        # Normalize unequal pixel scaling, then use measured horizontal focal length.
                        width=round(image_width*undistorter.new_K[1,1]/undistorter.new_K[0,0])
                        model_image=cv2.resize(corrected,(width,image_height))
                        focal_used=float(undistorter.new_K[0,0]*width/image_width)
                    tensor=transform(cv2.cvtColor(model_image,cv2.COLOR_BGR2RGB))
                    prediction=model.infer(tensor,f_px=None if focal_used is None else torch.tensor(focal_used,device=device))
                    if preview and estimated_focal is None:
                        estimated_focal=float(prediction['focallength_px'].item())
                        if not math.isfinite(estimated_focal) or estimated_focal<=0:
                            estimated_focal=None
                            raise ValueError('Depth Pro did not return a valid focal estimate')
                        focal_used=estimated_focal
                    result=cv2.resize(prediction['depth'].float().cpu().numpy(),(image_width,image_height),interpolation=cv2.INTER_LINEAR)
                else:
                    result=model.infer_image(corrected,input_size=294)
            summary=summarize_depth(result,mask)
            if args.engine=='depthanything':
                summary['distance_correction']=correct_center(summary['model_center_m'],profile,'vitb',294)
            else:
                summary['distance_correction']={'corrected_center_m':None,'reason':'legacy_wall_fit_not_applied','usable_for_grasp':False}
            summary['warning']='Estimated intrinsics: focal estimate and centered principal point; lens distortion uncorrected.' if preview else 'Metric accuracy within 1cm is not verified.'
            yy,xx=np.indices(result.shape)
            k=np.array([[focal_used,0,(image_width-1)/2],
                        [0,focal_used,(image_height-1)/2],[0,0,1]]) if preview else undistorter.new_K
            ranges=result*np.sqrt(1+((xx-k[0,2])/k[0,0])**2+((yy-k[1,2])/k[1,1])**2)
            display_max=1.0 if args.engine=='depthpro' else 3.0
            color=colorize_depth(ranges,mask,display_max)
            x,y,w,h=summary['roi']
            cv2.rectangle(color,(x,y),(x+w-1,y+h-1),(255,255,255),1)
            output=Path(command['output'])
            cv2.imwrite(str(output/'undistorted.jpg'),corrected)
            cv2.imwrite(str(output/'depth.jpg'),color)
            np.savez_compressed(str(output/'latest_depth.npz'),model_depth_m=result,
                                model_ray_range_m=ranges.astype('float32'),
                                valid_mask=mask,camera_matrix=k,
                                source_sequence=command['sequence'],source_received_at=command['source_received_at'],
                                calibration_source=calibration_source,calibration_id=calibration_id,metric_validated=False,
                                model_encoder=encoder,input_size=input_size,engine=args.engine,
                                profile_json=json.dumps(profile,ensure_ascii=False),
                                corrected_center_m=np.nan if summary['distance_correction']['corrected_center_m'] is None else summary['distance_correction']['corrected_center_m'])
            (output/'depth_values.bin').write_bytes(result.astype('<f4').tobytes())
            (output/'valid_values.bin').write_bytes(mask.astype('uint8').tobytes())
            print(json.dumps({'ok':True,'sequence':command['sequence'],'summary':summary,
                              'camera_matrix':k.tolist(),'preview':preview,'image_width':image_width,'image_height':image_height,
                              'calibration_id':calibration_id,'engine':args.engine,'encoder':encoder,'input_size':input_size,
                              'profile_id':profile['profile_id'],'focal_px_used':focal_used,
                              'visualization_max_m':display_max,'visualization_distance_kind':'camera_ray_range',
                              'inference_ms':round((time.monotonic()-started)*1000,1)}),flush=True)
        except Exception as error:
            print(json.dumps({'ok':False,'error':str(error)}),flush=True)


if __name__=='__main__':
    main()
