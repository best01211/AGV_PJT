"""실측 거리 CSV 평가. 보정 계수를 학습하거나 제어 허용을 자동 결정하지 않습니다."""
import argparse
import csv
import json
import math
from pathlib import Path


def evaluate(rows):
    samples = []
    for row in rows:
        gt, pred = float(row['ground_truth_m']), float(row['prediction_m'])
        if not math.isfinite(gt) or not math.isfinite(pred) or gt <= 0 or pred <= 0:
            raise ValueError('Distances must be finite and positive')
        samples.append((gt, pred))
    if not samples:
        raise ValueError('No measurements')
    errors = [pred-gt for gt,pred in samples]
    inversions = sum(1 for a in samples for b in samples if a[0] < b[0] and a[1] > b[1])
    return {'sample_count':len(samples),'mae_m':sum(abs(e) for e in errors)/len(errors),
            'bias_m':sum(errors)/len(errors),'rmse_m':math.sqrt(sum(e*e for e in errors)/len(errors)),
            'max_absolute_error_m':max(abs(e) for e in errors),'distance_order_inversions':inversions,
            'metric_validated':False,'usable_for_control':False,
            'note':'Order inversions cannot be repaired by a positive global scale. Independent scenes and held-out data are required.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True)
    args=parser.parse_args()
    with Path(args.input).open(encoding='utf-8-sig',newline='') as stream:
        print(json.dumps(evaluate(list(csv.DictReader(stream))),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
