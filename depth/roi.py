"""고정 프레임에서 지정한 물체 영역의 모델 원본 깊이를 요약합니다."""
import math
import statistics
import struct


def summarize_region(values, valid_mask, width, height, roi, camera_matrix=None):
    if len(values)!=width*height*4 or len(valid_mask)!=width*height:
        raise ValueError('Invalid depth buffer')
    x,y,w,h=roi
    if any(type(v) is not int for v in roi) or w<4 or h<4 or x<0 or y<0 or x+w>width or y+h>height:
        raise ValueError('ROI must be at least 4x4 pixels and inside the corrected image')
    samples=[]
    ranges=[]
    if camera_matrix is not None:
        fx,fy=camera_matrix[0][0],camera_matrix[1][1]
        cx,cy=camera_matrix[0][2],camera_matrix[1][2]
        if not all(math.isfinite(v) for v in (fx,fy,cx,cy)) or fx<=0 or fy<=0:
            raise ValueError('Invalid corrected camera intrinsics')
    for row in range(y,y+h):
        for col in range(x,x+w):
            index=row*width+col
            value=struct.unpack_from('<f',values,index*4)[0]
            if valid_mask[index] and math.isfinite(value) and 0<value<20:
                samples.append(value)
                if camera_matrix is not None:
                    ranges.append(value*math.sqrt(1+((col-cx)/fx)**2+((row-cy)/fy)**2))
    samples.sort()
    enough=len(samples)>=math.ceil(w*h*.5)
    median=statistics.median(samples) if enough else None
    p10=samples[int((len(samples)-1)*.1)] if enough else None
    p90=samples[int((len(samples)-1)*.9)] if enough else None
    ranges.sort()
    return {'roi':list(roi),'valid_pixels':len(samples),'total_pixels':w*h,
            'model_target_m':median,'p10_m':p10,'p90_m':p90,
            'model_depth_z_m':median,
            'model_ray_range_m':statistics.median(ranges) if enough and ranges else None,
            'range_p10_m':ranges[int((len(ranges)-1)*.1)] if enough and ranges else None,
            'range_p90_m':ranges[int((len(ranges)-1)*.9)] if enough and ranges else None,
            'mixed_depth_warning':enough and p90-p10>max(.1,median*.3),
            'wall_calibration_applied':False,'metric_validated':False,'usable_for_grasp':False,
            'note':'Selected object ROI; model estimate only. A narrow spread is not proof of correct absolute distance.'}
