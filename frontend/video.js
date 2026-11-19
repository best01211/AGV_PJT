const v = id => document.getElementById(id);
let active=true, objectUrl=null, seenSequence=null, requestInFlight=false, version=0;
let calibrationCount=null, capturePending=false;
let rangePending=false;
function updateCaptureButton(){
  v('capture-calibration').disabled=capturePending||seenSequence===null||calibrationCount===null||calibrationCount>=12;
  v('measure-board').disabled=rangePending||seenSequence===null;
}
function showCalibrationCount(result){
  if(!result.enabled)return;
  calibrationCount=result.count;
  v('calibration-capture').hidden=false;
  v('board-range').hidden=false;
  v('calibration-count').textContent=`저장 ${calibrationCount} / 12장`;
  if(calibrationCount>=12)v('calibration-message').textContent='촬영 완료. 보정용 사진 12장이 저장되었습니다.';
  updateCaptureButton();
}
const states={disconnected:'연결 대기',connecting:'연결 중',receiving:'프레임 수신 중',reconnecting:'재연결 중'};
function showVideoStatus(message, online = false) {
  const label = v('video-status');
  label.textContent = message;
  label.parentElement.classList.toggle('online', online);
}
async function cameraRequest(path,method='GET'){
  const response=await fetch('/api/camera'+path,{method,cache:'no-store',signal:AbortSignal.timeout(6000)});
  const result=await response.json();if(!response.ok)throw new Error(result.detail||'영상 요청 실패');return result;
}
function clearImage(message){v('camera-image').hidden=true;v('camera-image').removeAttribute('src');v('placeholder').hidden=false;v('placeholder').querySelector('strong').textContent=message;v('resolution').textContent='수신 프레임 없음';seenSequence=null;updateCaptureButton();if(objectUrl){URL.revokeObjectURL(objectUrl);objectUrl=null;}}
function displayState(state){
  v('configured').textContent=state.configured?'설정됨':'미설정';v('video-format').textContent=state.format||'—';
  v('last-frame').textContent=state.last_frame_at?new Date(state.last_frame_at*1000).toLocaleTimeString('ko-KR'):'—';v('frame-number').textContent=state.sequence||'—';
  showVideoStatus(state.frame_available && seenSequence !== null ? '영상 표시 중' : states[state.state] || '상태 확인 불가', state.frame_available);
  v('video-message').textContent=state.configuration_error||state.message;
  v('connect-video').disabled=!state.configured||['connecting','receiving'].includes(state.state);
  v('disconnect-video').disabled=state.state==='disconnected';
  if(!state.frame_available){clearImage(state.state==='disconnected'?'JETANK 영상 연결 대기':'최신 영상 프레임 대기');}
}
async function frame(state,currentVersion){
  if(!state.frame_available||seenSequence===state.sequence)return;
  const response=await fetch('/api/camera/frame',{cache:'no-store',signal:AbortSignal.timeout(4000)});
  if(!response.ok)throw new Error('최신 프레임을 받을 수 없습니다.');
  const blob=await response.blob();if(!active||currentVersion!==version)return;
  const url=URL.createObjectURL(blob),image=new Image();
  try{image.src=url;await image.decode();}catch{URL.revokeObjectURL(url);throw new Error('영상 프레임을 표시할 수 없습니다. JPEG 형식을 확인해 주세요.');}
  if(!active||currentVersion!==version){URL.revokeObjectURL(url);return;}
  const previous=objectUrl;objectUrl=url;v('camera-image').src=url;v('camera-image').hidden=false;v('placeholder').hidden=true;
  if(previous)URL.revokeObjectURL(previous);
  seenSequence=Number(response.headers.get('X-Frame-Sequence'));
  updateCaptureButton();
  v('resolution').textContent=`${image.naturalWidth} × ${image.naturalHeight}`;
  showVideoStatus('영상 표시 중', true);
}
async function poll(){
  if(active&&!requestInFlight){requestInFlight=true;const currentVersion=version;try{const state=await cameraRequest('/status');if(currentVersion===version){displayState(state);await frame(state,currentVersion);}}catch(error){if(currentVersion===version){clearImage('영상 수신 중단');showVideoStatus('연결 확인 필요');v('video-message').textContent=error.message;}}finally{requestInFlight=false;}}
  if(active)setTimeout(poll,250);
}
async function control(path){const currentVersion=++version;v('connect-video').disabled=true;try{const state=await cameraRequest(path,'POST');if(currentVersion===version)displayState(state);}catch(error){if(currentVersion===version){v('video-message').textContent=error.message;v('connect-video').disabled=false;}}}
v('connect-video').onclick=()=>control('/connect');v('disconnect-video').onclick=()=>control('/disconnect');
v('fullscreen-video').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await v('video-stage').requestFullscreen();}catch{v('video-message').textContent='이 브라우저에서는 전체 화면을 사용할 수 없습니다.';}};
window.addEventListener('pagehide',()=>{active=false;version++;if(objectUrl)URL.revokeObjectURL(objectUrl);});
window.addEventListener('pageshow',event=>{if(event.persisted){active=true;poll();}});poll();
v('capture-calibration').onclick=async()=>{
  capturePending=true;
  updateCaptureButton();
  v('calibration-message').textContent='마커 확인 중…';
  try{
    const response=await fetch('/api/camera/calibration-captures',{method:'POST',signal:AbortSignal.timeout(20000)});
    const result=await response.json();
    if(!response.ok)throw new Error(result.detail||'촬영에 실패했습니다.');
    v('calibration-message').textContent='저장했습니다. 마커판을 옮기거나 기울인 뒤 다시 촬영해 주세요.';
    showCalibrationCount(result);
  }catch(error){
    v('calibration-message').textContent=error.message;
  }finally{
    capturePending=false;
    updateCaptureButton();
  }
};
cameraRequest('/calibration-captures').then(showCalibrationCount).catch(()=>{});
v('measure-board').onclick=async()=>{
  rangePending=true;
  updateCaptureButton();
  v('board-result').textContent='거리 계산 중…';
  try{
    const result=await cameraRequest('/board-range','POST');
    v('board-result').textContent=`6번 마커 직선거리 ${result.range_cm.toFixed(2)} cm · 광축 깊이 Z ${result.depth_cm.toFixed(2)} cm · 프레임 #${result.sequence}`;
  }catch(error){
    v('board-result').textContent=error.message;
  }finally{
    rangePending=false;
    updateCaptureButton();
  }
};
