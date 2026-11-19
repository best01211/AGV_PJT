"""1m 이내 ±1cm 요구를 표본별 최대 오차로 검증. 평균 오차만으로 합격하지 않습니다."""
import math


def validate_nearfield(samples, tolerance_m=.01):
    if not samples or not math.isfinite(tolerance_m) or tolerance_m<=0:
        raise ValueError('Nonempty samples and positive finite tolerance are required')
    errors=[]
    ids=set()
    for sample in samples:
        identifier=sample['sample_id']
        if identifier in ids:
            raise ValueError('Duplicate validation sample')
        ids.add(identifier)
        gt,pred=sample['ground_truth_ray_m'],sample['prediction_ray_m']
        if not math.isfinite(gt) or not math.isfinite(pred) or not 0<gt<=1 or pred<=0:
            raise ValueError('Invalid or out-of-range validation distance')
        errors.append(abs(pred-gt))
    return {'sample_count':len(errors),'max_distance_m':1,'tolerance_m':tolerance_m,
            'mae_m':sum(errors)/len(errors),'max_absolute_error_m':max(errors),
            'passed_samples':sum(error<=tolerance_m+1e-9 for error in errors),
            'test_set_passed':all(error<=tolerance_m+1e-9 for error in errors),
            'observed_ground_truth_range_m':[min(s['ground_truth_ray_m'] for s in samples),max(s['ground_truth_ray_m'] for s in samples)],
            'full_range_verified':False,'usable_for_grasp':False}
