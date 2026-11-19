import { directions, ExplorationDemo } from './mapping-demo.js?v=agv-pose';
import { fitMap, setMap } from './mapping-view.js?v=agv-pose';

const el = id => document.getElementById(id);
const demo = new ExplorationDemo();
const timeFormat = new Intl.DateTimeFormat('ko-KR', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
const motionLabels = { idle: '이동 준비', moving: '이동 중', waiting: '경유지 대기',
  stopped: '정지됨', arrived: '도착 완료' };
const mapActions = ['initialize', 'reset', 'save-map', 'save-live-map', 'load-map'];
let demoMode = true;
let playback;
let liveMap;
let mappingError = '';
let mappingBusy = false;
let mappingGeneration = 0;
let driveBusy = false;

async function api(path, method = 'GET', payload) {
  const response = await fetch(path, {
    method, signal: AbortSignal.timeout(4000), cache: 'no-store',
    headers: payload === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = data.detail;
    const message = Array.isArray(detail) ? detail[0]?.msg : detail;
    throw Object.assign(new Error(message || '서버 응답을 확인해 주세요.'), { status: response.status });
  }
  return response.json();
}

function render() {
  const state = demoMode ? demo.state : liveMap;
  const now = mappingError && !demoMode ? Infinity : Date.now() / 1000;
  const lastUpdate = state?.last_update_s;
  const fresh = lastUpdate != null && now - lastUpdate <= state.config.max_age_s;
  const observed = state?.data.reduce((sum, value) => sum + Number(value >= 0), 0);
  const total = state?.data.length;
  const coverage = total ? observed / total * 100 : 0;
  const exploration = demoMode
    ? playback ? '탐색 중' : demo.blocked ? '이동 공간 부족' : demo.finished ? '시연 종료' : demo.step ? '일시정지' : '시작 대기'
    : mappingError ? '연결 오류' : fresh ? '관측 수신 중' : '관측 대기';

  el('map-badge').textContent = demoMode ? '시뮬레이션' : '실측 관측';
  el('exploration-status').textContent = exploration;
  el('map-description').textContent = demoMode ? '실내 테스트 구역 · 1층' : state
    ? state.config.frame_id + ' · 격자 ' + state.config.resolution_m + ' m' : '실측 지도 입력 대기';
  el('map-empty').hidden = Boolean(state);
  el('percentage').textContent = total ? coverage.toFixed(1) + '%' : '—';
  el('coverage').value = coverage;
  el('coverage-context').textContent = total
    ? observed.toLocaleString('ko-KR') + ' / ' + total.toLocaleString('ko-KR') + ' 셀 관측'
    : '관측된 셀 / 지도 전체 셀';
  el('accepted').textContent = state ? state.accepted.toLocaleString('ko-KR') : '—';
  el('updated').textContent = lastUpdate ? timeFormat.format(new Date(lastUpdate * 1000)) : '—';
  el('demo-controls').hidden = !demoMode;
  el('live-hint').hidden = demoMode;
  el('live-map-actions').hidden = demoMode;
  el('live-save-status').hidden = demoMode;
  el('save-live-map').disabled = mappingBusy || !liveMap || Boolean(mappingError);
  if (!demoMode) {
    el('live-hint').textContent = mappingError || '외부 수집기 관측을 자동 반영합니다. 거부 '
      + (state?.rejected ?? 0).toLocaleString('ko-KR') + '건 · 입력 ' + (state?.sources?.join(', ') || '대기');
  }
  el('play').textContent = playback ? '탐색 일시정지'
    : demo.finished ? '다시 탐색' : demo.step ? '탐색 재개' : '탐색 시작';
  el('sensor-description').textContent = demoMode ? '가상 반사 거리' : '최근 유효 반사 거리';
  el('camera-layer').disabled = !demoMode && !state?.config.camera_views?.length;
  el('camera-layer').parentElement.title = el('camera-layer').disabled
    ? '실측 카메라 시야 투영에는 장착 및 시야 보정값이 필요합니다.' : '지도에 센서 장착 기준의 기하학적 시야를 표시합니다.';
  for (const direction of directions) {
    const range = state?.latest_ranges?.[direction];
    const available = range && (demoMode || now - range.timestamp_s <= state.config.max_age_s);
    el('range-' + direction).textContent = available && range.distance_m != null
      ? range.distance_m.toFixed(2) + ' m' : demoMode && available ? '무응답' : '—';
  }
  const pose = state?.last_pose;
  el('pose-info').textContent = pose
    ? (demoMode ? '가상 위치' : fresh ? '최근 위치' : '마지막 위치')
      + ' · X ' + pose.x_m.toFixed(2) + ' / Y ' + pose.y_m.toFixed(2) + ' m'
    : '로봇 위치 수신 대기';
  el('mapping-heading').textContent = pose ? agvPose.heading(pose.yaw_rad) : '—';
  setMap(state, { demo: demoMode, now });
}

function switchMode(nextIsDemo) {
  if (nextIsDemo === demoMode) return;
  demoMode = nextIsDemo;
  stopPlayback();
  el('demo-mode').setAttribute('aria-pressed', String(demoMode));
  el('live-mode').setAttribute('aria-pressed', String(!demoMode));
  fitMap();
  render();
}

function stopPlayback() {
  clearInterval(playback);
  playback = undefined;
}

function startPlayback() {
  playback = setInterval(() => {
    demo.advance(2);
    if (demo.finished) stopPlayback();
    render();
  }, 150);
}

el('demo-mode').onclick = () => switchMode(true);
el('live-mode').onclick = () => { switchMode(false); void getLiveMap(); };
el('play').onclick = () => {
  if (playback) stopPlayback();
  else {
    if (demo.finished) demo.reset();
    startPlayback();
  }
  render();
};
el('restart').onclick = () => {
  stopPlayback();
  demo.reset();
  startPlayback();
  render();
};

async function getLiveMap() {
  if (demoMode || mappingBusy) return;
  const generation = ++mappingGeneration;
  try {
    const state = await api('/api/mapping/state');
    if (generation !== mappingGeneration) return;
    liveMap = state;
    mappingError = '';
  } catch (error) {
    if (generation !== mappingGeneration) return;
    if (error.status === 409) liveMap = undefined;
    mappingError = error.status === 409 ? '' : '실측 지도 관측 연결을 확인해 주세요.';
  }
  if (!demoMode) render();
}

async function withMapAction(work) {
  if (mappingBusy) return;
  mappingBusy = true;
  mappingGeneration++;
  for (const id of mapActions) el(id).disabled = true;
  try {
    el('map-message').textContent = await work();
  } catch (error) {
    el('map-message').textContent = error.message;
  } finally {
    mappingBusy = false;
    for (const id of mapActions) el(id).disabled = false;
    render();
  }
}

function showLiveMap(state) {
  liveMap = state;
  mappingError = '';
  switchMode(false);
  fitMap();
  render();
}

function showSavedMapLink(id) {
  for (const linkId of ['saved-map-to-drive', 'saved-map-settings-to-drive']) {
    const link = el(linkId);
    link.hidden = !id;
    if (id) link.href = '/?sensor_map=' + encodeURIComponent(id) + '#sensor-map-import';
    else link.removeAttribute('href');
  }
  el('live-save-status').textContent = id ? '저장 지도 ID ' + id : '';
}

el('initialize').onclick = () => withMapAction(async () => {
  showLiveMap(await api('/api/mapping/initialize', 'POST', JSON.parse(el('config').value)));
  showSavedMapLink(null);
  return '빈 지도를 생성했습니다. 센서 관측을 기다립니다.';
});
el('reset').onclick = () => withMapAction(async () => {
  const config = JSON.parse(el('config').value);
  if (!confirm('저장하지 않은 실측 지도를 빈 지도로 교체할까요?')) return '지도 초기화를 취소했습니다.';
  showLiveMap(await api('/api/mapping/reset', 'POST', config));
  showSavedMapLink(null);
  return '실측 지도를 초기화했습니다.';
});
function saveLiveMap() {
  return withMapAction(async () => {
    el('live-save-status').textContent = '관측 지도를 저장하는 중입니다.';
    try {
      const saved = await api('/api/mapping/save', 'POST', {});
      el('map-id').value = saved.map_id;
      showSavedMapLink(saved.map_id);
      return '실측 지도를 저장했습니다. 주행 설정에서 이 지도를 불러올 수 있습니다.';
    } catch (error) {
      el('live-save-status').textContent = error.message;
      throw error;
    }
  });
}
el('save-map').onclick = el('save-live-map').onclick = saveLiveMap;
el('load-map').onclick = () => withMapAction(async () => {
  const id = el('map-id').value.trim();
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('32자리 저장 지도 ID를 입력하세요.');
  if (!confirm('현재 실측 지도를 저장된 지도로 교체할까요?')) return '지도 교체를 취소했습니다.';
  const state = await api('/api/mapping/load/' + id, 'POST', {});
  el('config').value = JSON.stringify(state.config, null, 2);
  showLiveMap(state);
  showSavedMapLink(id);
  return '저장 지도를 불러왔습니다. 새 관측을 받으면 로봇 위치가 표시됩니다.';
});

const cameraFeeds = [ ['camera-a', '/api/camera'], ['camera-b', '/api/camera/secondary'] ].map(([id, base]) => {
  const figure = el(id);
  const image = figure.querySelector('img');
  const button = figure.querySelector('button');
  const label = figure.querySelector('[data-camera-status]');
  let busy = false;
  let version = 0;
  let sequence = null;
  let connected = false;

  function display(status) {
    label.textContent = status.frame_available ? '영상 수신 중'
      : !status.configured ? '영상 주소 미설정'
        : ({ connecting: '연결 중', receiving: '최신 영상 대기', reconnecting: '재연결 중',
          disconnected: '연결 대기' }[status.state] || '영상 수신 대기');
    label.title = status.configuration_error || status.message || '';
    connected = status.configured && status.state !== 'disconnected';
    button.textContent = connected ? '영상 연결 해제' : '영상 연결';
    button.disabled = busy || !status.configured;
    if (status.frame_available && sequence !== status.sequence) {
      sequence = status.sequence;
      image.src = base + '/frame?sequence=' + sequence;
    } else if (!status.frame_available) {
      sequence = null;
      image.hidden = true;
      image.removeAttribute('src');
    }
  }

  image.onload = () => { image.hidden = false; };
  image.onerror = () => { image.hidden = true; sequence = null; };
  button.onclick = async () => {
    busy = true;
    version++;
    button.disabled = true;
    try {
      const status = await api(base + (connected ? '/disconnect' : '/connect'), 'POST');
      busy = false;
      display(status);
    } catch (error) {
      busy = false;
      label.textContent = error.message;
      button.disabled = false;
    }
  };
  return async () => {
    if (busy) return;
    const snapshotVersion = version;
    try {
      const status = await api(base + '/status');
      if (snapshotVersion === version) display(status);
    } catch {
      if (snapshotVersion !== version) return;
      image.hidden = true;
      image.removeAttribute('src');
      sequence = null;
      label.textContent = '영상 상태 확인 실패';
      button.disabled = true;
    }
  };
});

const destination = el('drive-destination');
let lastDestinations;
let driveGeneration = 0;
let driveOnline = false;
let driveDemoAvailable = false;

function renderDrive(state) {
  driveOnline = Boolean(state);
  driveDemoAvailable = Boolean(state && !state.navigation_map);
  el('drive-demo').hidden = !driveDemoAvailable;
  el('drive-demo').disabled = driveBusy;
  el('server-status').textContent = state ? '서버 연결됨' : '서버 연결 끊김';
  el('server-status').parentElement.classList.toggle('online', driveOnline);
  el('drive-state').textContent = state
    ? (state.navigation_map ? motionLabels[state.status] || '상태 확인 중' : '주행 지도 등록 대기')
      + (state.target ? ' · ' + state.target : '')
    : '주행 서버 연결 실패';
  const names = Object.keys(state?.destinations ?? {});
  const key = JSON.stringify(names);
  if (key !== lastDestinations) {
    const selected = destination.value;
    destination.replaceChildren(new Option(names.length ? '목적지 선택' : '지도 등록 대기', ''));
    for (const name of names) destination.add(new Option(name, name));
    if (names.includes(selected)) destination.value = selected;
    lastDestinations = key;
  }
  destination.disabled = driveBusy || !names.length;
  el('drive-send').disabled = driveBusy || !destination.value;
  el('drive-stop').disabled = !driveOnline || driveBusy;
}

async function pollDrive() {
  const generation = ++driveGeneration;
  try {
    const state = await api('/api/state');
    if (generation === driveGeneration && !driveBusy) renderDrive(state);
  } catch {
    if (generation === driveGeneration && !driveBusy) renderDrive(null);
  }
}

async function driveCommand(path, payload) {
  if (driveBusy || !driveOnline) return;
  driveBusy = true;
  driveGeneration++;
  destination.disabled = true;
  el('drive-send').disabled = true;
  el('drive-stop').disabled = true;
  try {
    const result = await api(path, 'POST', payload);
    el('drive-result').textContent = result.message || (result.accepted ? '명령을 수락했습니다.' : '명령이 거부되었습니다.');
  } catch (error) {
    el('drive-result').textContent = error.message;
  } finally {
    driveBusy = false;
    await pollDrive();
  }
}

destination.onchange = () => { el('drive-send').disabled = driveBusy || !driveOnline || !destination.value; };
el('drive-form').onsubmit = event => {
  event.preventDefault();
  if (destination.value) void driveCommand('/api/commands', { text: destination.value + '로 이동해줘' });
};
el('drive-stop').onclick = () => driveCommand('/api/stop');
el('drive-demo').onclick = async () => {
  if (driveBusy || !driveDemoAvailable) return;
  driveBusy = true;
  driveGeneration++;
  el('drive-demo').disabled = true;
  el('drive-stop').disabled = true;
  try {
    const map = await api('/static/navigation-demo.json');
    await api('/api/navigation/map', 'PUT', map);
    el('drive-result').textContent = '예제 지도를 열었습니다.';
  } catch (error) {
    el('drive-result').textContent = error.message;
  } finally {
    driveBusy = false;
    await pollDrive();
  }
};

async function refresh() {
  await Promise.all([getLiveMap(), pollDrive(), ...cameraFeeds.map(update => update())]);
  setTimeout(refresh, 800);
}

render();
void refresh();
