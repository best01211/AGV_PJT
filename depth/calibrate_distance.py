"""동일 장면의 중심 거리 실험용 affine profile. Dense depth/주행 제어에 자동 적용하지 않음."""
import argparse
import csv
import json
import math
from pathlib import Path


def fit(points):
    if len(points)<3 or len({y for x,y in points})<3:
        raise ValueError('At least three distinct ground-truth distances are required')
    if any(not math.isfinite(x) or not math.isfinite(y) or x<=0 or y<=0 for x,y in points):
        raise ValueError('Distances must be finite and positive')
    mx=sum(x for x,y in points)/len(points)
    my=sum(y for x,y in points)/len(points)
    denominator=sum((x-mx)**2 for x,y in points)
    if denominator<=1e-12:
        raise ValueError('Predictions have insufficient variation')
    scale=sum((x-mx)*(y-my) for x,y in points)/denominator
    if scale<=0:
        raise ValueError('A positive monotonic fit is required')
    return scale,my-scale*mx


def summarize_errors(errors):
    return {'mae_m':sum(abs(e) for e in errors)/len(errors),
            'max_absolute_error_m':max(abs(e) for e in errors)}


def build_profile(rows,encoder,input_size):
    selected=[r for r in rows if r['encoder']==encoder and int(r['input_size'])==input_size]
    if len(selected)<4:
        raise ValueError('At least four samples are needed for leave-one-out diagnostics')
    if len({r['scene'] for r in selected})!=1:
        raise ValueError('This profile is limited to one scene label')
    if len({r.get('roi') or '[140, 140, 20, 20]' for r in selected})!=1 or len({r.get('measurement_kind') or 'center' for r in selected})!=1:
        raise ValueError('Do not mix center and selected-target calibration observations')
    if len({r.get('distance_kind') or 'unspecified' for r in selected})!=1:
        raise ValueError('Do not mix optical Z and ray-range measurements')
    points=[(float(r['prediction_m']),float(r['ground_truth_m'])) for r in selected]
    if any(x1>x2 for x1,y1 in points for x2,y2 in points if y1<y2):
        raise ValueError('Distance order inversion: a global positive affine fit is inappropriate')
    scale,offset=fit(points)
    errors=[scale*x+offset-y for x,y in points]
    loo=[]
    for index,(x,y) in enumerate(points):
        a,b=fit(points[:index]+points[index+1:])
        loo.append(a*x+b-y)
    return {'schema_version':1,'encoder':encoder,'input_size':input_size,
            'scene':selected[0]['scene'],'sample_ids':[r['sample_id'] for r in selected],
            'scale':scale,'offset_m':offset,
            'prediction_domain_m':[min(x for x,y in points),max(x for x,y in points)],
            'ground_truth_domain_m':[min(y for x,y in points),max(y for x,y in points)],
            'training_error':summarize_errors(errors),'leave_one_out_diagnostic':summarize_errors(loo),
            'enabled':False,'metric_validated':False,'usable_for_control':False,
            'apply_to_dense_depth':False,
            'warning':'Same four samples used to select the model and fit the profile. LOO is not independent validation. Center ROI, recorded scene and calibrated stream only; no extrapolation.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--predictions',required=True)
    parser.add_argument('--encoder',required=True,choices=['vits','vitb','vitl'])
    parser.add_argument('--input-size',type=int,required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    with Path(args.predictions).open(encoding='utf-8-sig',newline='') as stream:
        profile=build_profile(list(csv.DictReader(stream)),args.encoder,args.input_size)
    Path(args.output).write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(profile,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
