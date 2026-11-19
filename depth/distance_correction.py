"""장면·모델에 제한된 중앙 거리 보정. 픽셀 전체 깊이나 로봇 제어를 보정하지 않습니다."""
import math


def correct_center(prediction, profile, encoder, input_size):
    result={'corrected_center_m':None,'profile_id':profile.get('profile_id'),
            'scope':profile.get('scene'),'usable_for_grasp':False,'usable_for_control':False,
            'validation_mae_m':profile.get('leave_one_out_diagnostic',{}).get('mae_m'),
            'validation_max_error_m':profile.get('leave_one_out_diagnostic',{}).get('max_absolute_error_m'),
            'reason':None}
    if not profile.get('enabled',False):
        result['reason']='profile_disabled'
    elif not math.isfinite(profile['scale']) or profile['scale']<=0 or not math.isfinite(profile['offset_m']):
        result['reason']='invalid_profile'
    elif encoder!=profile['encoder'] or input_size!=profile['input_size']:
        result['reason']='model_mismatch'
    elif prediction is None or not math.isfinite(prediction):
        result['reason']='invalid_depth'
    elif not profile['prediction_domain_m'][0]<=prediction<=profile['prediction_domain_m'][1]:
        result['reason']='outside_calibrated_range'
    else:
        corrected=profile['scale']*prediction+profile['offset_m']
        if corrected<=0 or not math.isfinite(corrected):
            result['reason']='invalid_correction'
        else:
            result['corrected_center_m']=corrected
            result['reason']='scene_specific_estimate_not_grasp_validated'
    return result
