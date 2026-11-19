"""Affine/2차식을 동일 LOOCV로 비교하고 실험용 affine 프로파일 작성."""
import argparse
import csv
import json
from pathlib import Path

import numpy as np

from depth.calibrate_distance import build_profile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--predictions',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    with Path(args.predictions).open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    rows=[r for r in rows if r['encoder']=='vitb' and int(r['input_size'])==294]
    profile=build_profile(rows,'vitb',294)
    x=np.array([float(r['prediction_m']) for r in rows])
    y=np.array([float(r['ground_truth_m']) for r in rows])
    comparisons={}
    for name,degree in [('affine',1),('quadratic',2)]:
        errors=[]
        for index in range(len(x)):
            keep=np.arange(len(x))!=index
            coefficients=np.polyfit(x[keep],y[keep],degree)
            errors.append(float(np.polyval(coefficients,x[index])-y[index]))
        comparisons[name]={'loo_mae_m':float(np.mean(np.abs(errors))),
                           'loo_max_error_m':float(np.max(np.abs(errors)))}
    profile.update(profile_id='wood_wall_base294_7points_v1',enabled=True,
                   enabled_scope='experimental_center_display_only',fit_comparison=comparisons,
                   selection_reason='Affine has lower LOO mean and maximum error than quadratic on this dataset.',
                   warning='All seven observations now used for fitting. Prior three held-out observations are no longer a test set. This is scene-specific center calibration, not validated grasp depth.')
    Path(args.output).write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(profile,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
