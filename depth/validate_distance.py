"""고정된 실험 보정식으로 학습에 사용하지 않은 관측만 평가합니다."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


def validate(profile, rows):
    scale,offset=profile['scale'],profile['offset_m']
    if not math.isfinite(scale) or not math.isfinite(offset) or scale<=0:
        raise ValueError('Invalid fixed profile')
    training=set(profile['sample_ids'])
    samples=[]
    for row in rows:
        if row['encoder']!=profile['encoder'] or int(row['input_size'])!=profile['input_size'] or row['sample_id'] in training:
            continue
        if row['scene']!=profile['scene']:
            raise ValueError('Scene differs from this scene-specific profile')
        gt,pred=float(row['ground_truth_m']),float(row['prediction_m'])
        if not math.isfinite(gt) or not math.isfinite(pred) or gt<=0 or pred<=0:
            raise ValueError('Invalid measurement')
        if not profile['prediction_domain_m'][0]<=pred<=profile['prediction_domain_m'][1]:
            raise ValueError('Observation is outside the profile domain; no extrapolation')
        corrected=scale*pred+offset
        samples.append({'sample_id':row['sample_id'],'ground_truth_m':gt,'base_raw_m':pred,
                        'corrected_m':corrected,'error_m':corrected-gt})
    if not samples:
        raise ValueError('No held-out observations')
    if len({s['sample_id'] for s in samples})!=len(samples):
        raise ValueError('Duplicate observations')
    errors=[s['error_m'] for s in samples]
    return {'profile_unchanged':True,'training_sample_ids':sorted(training),'validation_samples':samples,
            'sample_count':len(samples),'mae_m':sum(abs(e) for e in errors)/len(errors),
            'max_absolute_error_m':max(abs(e) for e in errors),'bias_m':sum(errors)/len(errors),
            'metric_validated':False,'usable_for_control':False,
            'scope':'Held-out observations of the same scene; not validation of dense depth or new materials.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--predictions',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    data=Path(args.profile).read_bytes()
    with Path(args.predictions).open(encoding='utf-8-sig',newline='') as stream:
        report=validate(json.loads(data),list(csv.DictReader(stream)))
    report['profile_sha256']=hashlib.sha256(data).hexdigest()
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
