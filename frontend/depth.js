const d=id=>document.getElementById(id);
let depthActive=true,depthResult=null,depthRun=null,depthSeen=null,depthGeneration=0;
let targetSnapshot=null,dragStart=null,measurementBusy=false;
const depthUrls={};
async function depthApi(path,body,timeout=6000){
  const response=await fetch('/api/depth'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',signal:AbortSignal.timeout(timeout)});
  const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));return result;
}
// C270 wall trial: fit frames 6587 and 6668, held-out frame 7640.
const c270WallTrial={
  profile:'depthpro_estimated_intrinsics_v1',focal:802.0863037109375,
  low:0.8038771548816759,high:0.9973775025624995,offset:0.20062732872208766
};
function showWallTrial(){
  const output=d('wall-trial-result');
  if(!d('wall-trial-enabled').checked){output.textContent='';return;}
  const frame=depthResult,raw=targetSnapshot?.probe?.model_ray_range_m;
  if(!frame?.preview||frame.profile_id!==c270WallTrial.profile||
     frame.image_width!==640||frame.image_height!==480||
     !Number.isFinite(frame.focal_px_used)||Math.abs(frame.focal_px_used-c270WallTrial.focal)>.001){
    output.textContent='시험 보정에는 C270 640×480과 추정 초점거리 802.086px가 필요합니다.';
    return;
  }
  if(!Number.isFinite(raw)){output.textContent='보정값을 보려면 같은 벽면의 영역을 선택하세요.';return;}
  if(raw<c270WallTrial.low||raw>c270WallTrial.high){
    output.textContent='시험 보정 범위 밖입니다. 원본 추정값을 확인하세요.';
    return;
  }
  output.textContent=`벽면 시험 보정 ${((raw-c270WallTrial.offset)*100).toFixed(2)} cm · 원본 ${(raw*100).toFixed(2)} cm · 70cm 검증 오차 +1.70~+8.08 cm`;
}
d('wall-trial-enabled').addEventListener('change',showWallTrial);

function batchCalibrationResult(batch){
  // Fixed using the 60 cm and 80 cm batch medians. The 70 cm batch was held out.
  const low=0.49122321788004053,high=1.0623439357300515;
  if(batch.scene.trim()!=='나무색 벽, 중앙')return '';
  if(batch.profile_id!=='depthpro_estimated_intrinsics_v1'||batch.calibration_id!==''||
     batch.focal_px_used!==609.0258178710938){
    return ' · 시험식 보류: 보정에 사용한 C270 초점거리와 다릅니다.';
  }
  if(batch.median_m<low||batch.median_m>high){
    return ' · 시험식 보류: 원본 중앙값이 보정 범위 밖입니다.';
  }
  const correct=value=>.6+(value-low)*.2/(high-low);
  const corrected=correct(batch.median_m);
  const error=Math.abs(corrected-batch.ground_truth_m);
  const passing=batch.frames.filter(frame=>frame.prediction_m>=low&&frame.prediction_m<=high&&
    Math.abs(correct(frame.prediction_m)-batch.ground_truth_m)<=.01+1e-9).length;
  return ` · 벽면 시험 보정 ${(corrected*100).toFixed(2)} cm · 중앙값 오차 ${(error*100).toFixed(2)} cm (${error<=.01+1e-9?'±1 cm 이내':'±1 cm 초과'}) · 개별 프레임 ${passing}/${batch.frames.length}장 기준 충족`;
}
function resetTarget(message='표적 선택 해제 · 기본 중앙 영역 측정'){
  targetSnapshot=null;dragStart=null;d('target-stage').classList.remove('is-selecting');d('target-box').hidden=true;d('target-result').textContent=message;showWallTrial();
}
function updateSaveButton(){
  const targetMode=d('measurement-mode').value==='target';
  let reason='';
  if(measurementBusy)reason='기록을 저장하는 중입니다.';
  else if(targetMode&&!targetSnapshot?.token)reason='먼저 표적 화면을 고정하세요.';
  else if(targetMode&&!targetSnapshot?.probe)reason='고정한 영상에서 물체 안쪽을 드래그하고 추정값을 확인하세요.';
  else if(!targetMode&&depthResult?.preview)reason='C270 실측은 물체 영역 모드에서 기록하세요.';
  else if(!targetMode&&(targetSnapshot||depthResult?.summary.model_center_m==null))reason='최신 중앙 거리 결과를 기다리는 중입니다.';
  else if(!d('measurement-scene').value.trim())reason='대상 이름을 입력하세요.';
  else if(!d('measurement-distance').value||!d('measurement-distance').validity.valid)reason='실측 거리와 단위를 확인하세요.';
  d('save-measurement').disabled=!!reason;
  d('save-batch').disabled=!!reason||!targetMode||!depthResult?.capture_id;
  showWallTrial();
  d('measurement-ready').textContent=reason||(targetMode&&!depthResult?.capture_id?'한 장 기록 가능 · 8장 기록에는 촬영 ID가 필요합니다.':'현재 결과를 기록할 수 있습니다.');
}
function measuredMeters(){return Number(d('measurement-distance').value)*(d('measurement-unit').value==='cm'?.01:1);}
function showMeasurementPreview(){
  const input=d('measurement-distance'),isCm=d('measurement-unit').value==='cm';
  input.min=isCm?'1':'0.01';
  input.max=isCm?'2000':'20';
  const value=measuredMeters();
  d('measurement-preview').textContent=input.value&&input.validity.valid?`기록할 실측 거리: ${(value*100).toFixed(1)} cm = ${value.toFixed(3)} m`:'거리를 입력해 단위를 확인하세요.';
  updateSaveButton();
}
d('measurement-distance').addEventListener('input',showMeasurementPreview);
d('measurement-unit').addEventListener('change',showMeasurementPreview);
d('measurement-scene').addEventListener('input',updateSaveButton);
d('measurement-mode').addEventListener('change',()=>{depthGeneration++;resetTarget(d('measurement-mode').value==='target'?'물체 영역 모드: 표적 화면 고정 후 드래그하세요.':'중앙 영역 모드: 중앙 흰 사각형 대상의 거리를 기록합니다.');depthSeen=null;updateSaveButton();});
function hideDepth(){
  for(const kind of ['depth','undistorted']){d(kind+'-image').hidden=true;if(depthUrls[kind]){URL.revokeObjectURL(depthUrls[kind]);delete depthUrls[kind];}}
  depthSeen=null;depthResult=null;d('depth-center').textContent='—';d('depth-correction').textContent='—';d('download-depth').hidden=true;d('freeze-target').disabled=true;updateSaveButton();
}
async function depthPoll(){
  const generation=depthGeneration;
  try{
    const state=await depthApi('/status');
    if(generation===depthGeneration){
      d('depth-calibration').textContent=state.calibration_available?'보정 파일 · '+state.calibration_source:state.preview_allowed?'C270 추정 초점거리 사용 · 렌즈 왜곡 보정 및 거리 검증 전':'카메라 보정 파일이 필요합니다 · '+state.calibration_source;
      d('depth-message').textContent=state.result_available&&state.result.preview?'C270 실시간 Depth 추정 중':state.message;d('start-depth').disabled=['loading','running'].includes(state.state);d('stop-depth').disabled=state.state==='stopped';
      if(targetSnapshot&&(state.run_id!==targetSnapshot.run||state.state!=='running'||Date.now()>targetSnapshot.expiresAt))resetTarget('표적 고정이 만료됐거나 Depth가 재시작됐습니다. 다시 선택해 주세요.');
      if(!targetSnapshot){
        if(!state.result_available)hideDepth();
        else if(state.run_id+':'+state.result.sequence!==depthSeen){
          const images=await Promise.all(['undistorted','depth'].map(async kind=>{
            const response=await fetch(`/api/depth/frame/${kind}?run_id=${state.run_id}&sequence=${state.result.sequence}`,{cache:'no-store',signal:AbortSignal.timeout(4000)});
            if(!response.ok)throw new Error('최신 Depth 영상 대기 중');
            return {kind,blob:await response.blob(),sequence:response.headers.get('X-Depth-Sequence'),run:response.headers.get('X-Depth-Run')};
          }));
          if(generation===depthGeneration&&!targetSnapshot&&images.every(image=>image.sequence===String(state.result.sequence)&&image.run===String(state.run_id))){
            for(const image of images){const previous=depthUrls[image.kind];depthUrls[image.kind]=URL.createObjectURL(image.blob);d(image.kind+'-image').src=depthUrls[image.kind];d(image.kind+'-image').hidden=false;if(previous)URL.revokeObjectURL(previous);}
            depthSeen=state.run_id+':'+state.result.sequence;depthResult=state.result;depthRun=state.run_id;
            const aspect=(depthResult.image_width||300)+' / '+(depthResult.image_height||300);
            document.querySelectorAll('.depth-stage').forEach(stage=>stage.style.aspectRatio=aspect);
            const center=depthResult.summary.model_center_m,correction=depthResult.summary.distance_correction;
            d('depth-input-title').textContent=depthResult.preview?'C270 원본 영상':'렌즈 왜곡 보정 · alpha=0';
            d('depth-model-name').textContent=depthResult.preview?'Depth Pro · 추정 내참수':depthResult.engine==='depthpro'?'Depth Pro · 보정된 초점거리 사용':'Depth Anything V2 · Base';
            d('depth-color-scale').textContent= `${depthResult.preview?'추정 렌즈 직선거리':'렌즈 직선거리'} 시각화 0~${depthResult.visualization_max_m||3}m · 가까움: 빨강 / 멂: 파랑`;
            d('depth-center').textContent=center===null?'유효 픽셀 부족':center.toFixed(3)+' m (모델 추정 · 거리 미검증)';
            d('depth-correction').textContent=depthResult.engine==='depthpro'?'이전 벽 보정 미적용':correction?.corrected_center_m==null?'시험 보정 범위 밖 · 거리값 제공 안 함':`${correction.corrected_center_m.toFixed(3)} m · 나무색 벽 중앙 기준 시험 보정`;
            d('depth-timing').textContent=`프레임 #${depthResult.sequence} · 처리 ${depthResult.inference_ms} ms · ${depthResult.encoder||'vits'} / 입력 ${depthResult.input_size||294}${depthResult.preview?' · 추정 초점거리 '+depthResult.focal_px_used.toFixed(1)+' px':''}`;
            d('download-depth').hidden=false;d('download-depth').href=`/api/depth/frame/array?run_id=${depthRun}&sequence=${depthResult.sequence}`;
            d('freeze-target').disabled=false;updateSaveButton();
          }
        }
      }
    }
  }catch(error){if(generation===depthGeneration){if(!targetSnapshot)hideDepth();d('depth-message').textContent=error.message;}}
  if(depthActive)setTimeout(depthPoll,500);
}
d('start-depth').onclick=async()=>{depthGeneration++;resetTarget();try{d('start-depth').disabled=true;const state=await depthApi('/start',{});d('depth-message').textContent=state.message;}catch(error){d('depth-message').textContent=error.message;d('start-depth').disabled=false;}};
d('stop-depth').onclick=async()=>{depthGeneration++;resetTarget();hideDepth();try{await depthApi('/stop',{});}catch(error){d('depth-message').textContent=error.message;}};
d('freeze-target').onclick=async()=>{
  if(!depthResult)return;
  d('measurement-mode').value='target';
  const selected={run:depthRun,sequence:depthResult.sequence,width:depthResult.image_width||300,height:depthResult.image_height||300,expiresAt:Date.now()+120000,token:null,probe:null,roi:null};
  depthGeneration++;targetSnapshot=selected;d('freeze-target').disabled=true;d('target-box').hidden=true;d('target-result').textContent='프레임 고정 중…';updateSaveButton();
  try{const result=await depthApi('/pin',{run_id:selected.run,sequence:selected.sequence});if(targetSnapshot!==selected)return;selected.token=result.token;d('target-stage').classList.add('is-selecting');updateSaveButton();d('target-result').textContent=`프레임 #${selected.sequence} 고정됨. 보정 영상에서 물체 안쪽을 드래그하세요.`;d('target-stage').scrollIntoView({block:'center'});}catch(error){if(targetSnapshot===selected){resetTarget(error.message);d('freeze-target').disabled=false;updateSaveButton();}}
};
d('resume-target').onclick=()=>{depthGeneration++;resetTarget();depthSeen=null;updateSaveButton();};
function pixel(event){const rect=d('target-stage').getBoundingClientRect(),{width,height}=targetSnapshot;return [Math.max(0,Math.min(width-1,Math.floor((event.clientX-rect.left-1)/(rect.width-2)*width))),Math.max(0,Math.min(height-1,Math.floor((event.clientY-rect.top-1)/(rect.height-2)*height)))];}
function drawRegion(start,end){const [x,y]=[Math.min(start[0],end[0]),Math.min(start[1],end[1])],w=Math.abs(start[0]-end[0])+1,h=Math.abs(start[1]-end[1])+1;Object.assign(d('target-box').style,{left:100*x/targetSnapshot.width+'%',top:100*y/targetSnapshot.height+'%',width:100*w/targetSnapshot.width+'%',height:100*h/targetSnapshot.height+'%'});d('target-box').hidden=false;return {x,y,width:w,height:h};}
d('target-stage').onpointerdown=event=>{if(!targetSnapshot?.token){d('target-result').textContent='먼저 표적 화면 고정을 눌러 주세요.';return;}event.preventDefault();dragStart=pixel(event);targetSnapshot.probe=null;targetSnapshot.selectionVersion=null;updateSaveButton();d('target-stage').setPointerCapture(event.pointerId);drawRegion(dragStart,dragStart);};
d('target-stage').onpointermove=event=>{if(dragStart)drawRegion(dragStart,pixel(event));};
d('target-stage').onpointerup=async event=>{
  if(!dragStart||!targetSnapshot?.token)return;
  const selected=targetSnapshot,roi=drawRegion(dragStart,pixel(event));dragStart=null;selected.roi=roi;
  if(roi.width<4||roi.height<4){d('target-result').textContent='물체 안쪽을 4×4픽셀 이상 선택해 주세요.';return;}
  const selectionVersion=Symbol();selected.selectionVersion=selectionVersion;
  try{const result=await depthApi('/probe',{token:selected.token,...roi});if(targetSnapshot!==selected||selected.selectionVersion!==selectionVersion)return;selected.probe=result.model_ray_range_m===null?null:result;d('target-result').textContent=result.model_ray_range_m===null?'직선거리 계산에 필요한 깊이 또는 내참수가 없습니다. Depth를 다시 시작해 주세요.':`선택 물체 직선거리 추정 ${result.model_ray_range_m.toFixed(3)} m · 광축 깊이 Z ${result.model_depth_z_m.toFixed(3)} m · 직선거리 분포 10~90%: ${result.range_p10_m.toFixed(3)}~${result.range_p90_m.toFixed(3)} m · ${result.mixed_depth_warning?'깊이 차이가 커 배경 혼입 가능':'영역 분포 확인'} · 원본 거리 / 절대거리 미검증`;updateSaveButton();}catch(error){if(targetSnapshot===selected)d('target-result').textContent=error.message;}
};
d('target-stage').onpointercancel=()=>{dragStart=null;};
d('measurement-form').onsubmit=async event=>{
  event.preventDefault();if(!depthResult||measurementBusy)return;
  const selected=targetSnapshot;measurementBusy=true;updateSaveButton();
  try{
    const common={scene:d('measurement-scene').value,ground_truth_m:measuredMeters()};
    if(event.submitter?.id==='save-batch'){
      d('measurement-message').textContent='새 프레임 8장 수집 중 · 카메라와 표적을 그대로 유지하세요.';
      const batch=await depthApi('/target-batches',{...common,token:selected.token,...selected.roi},32000);
      const cm=value=>(value*100).toFixed(2);
      d('measurement-message').textContent=`8장 기록 완료 · 실측 ${cm(batch.ground_truth_m)} cm · 중앙값 ${cm(batch.median_m)} cm · 범위 ${cm(batch.min_m)}~${cm(batch.max_m)} cm`+batchCalibrationResult(batch);
      return;
    }
    let saved;
    if(d('measurement-mode').value==='target'&&!selected?.probe)throw new Error('물체 영역 모드입니다. 먼저 표적 화면 고정 후 물체 안쪽을 드래그하세요.');
    if(selected){if(!selected.token||!selected.probe)throw new Error('먼저 물체 영역을 선택하세요.');saved=await depthApi('/target-measurements',{...common,token:selected.token,distance_kind:'camera_ray_range',...selected.roi});}
    else if(depthResult.preview)throw new Error('C270은 물체 영역 모드로 실측 직선거리를 기록해 주세요.');
    else saved=await depthApi('/measurements',{...common,sequence:depthResult.sequence,run_id:depthRun});
    d('measurement-message').textContent=`기록 완료: ${selected?'선택 물체':'중앙 영역'} · 실측 ${saved.ground_truth_m} m / 원본 추정 ${saved.prediction_m.toFixed(3)} m`;
  }catch(error){d('measurement-message').textContent=error.message;}finally{measurementBusy=false;updateSaveButton();}
};
window.addEventListener('pagehide',()=>{depthActive=false;depthGeneration++;resetTarget();hideDepth();});window.addEventListener('pageshow',event=>{if(event.persisted){depthActive=true;depthPoll();}});depthPoll();
