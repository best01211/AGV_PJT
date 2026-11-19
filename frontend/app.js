const $ = id => document.getElementById(id);
const labels = {idle:'이동 준비 완료',moving:'이동 중',waiting:'경유지에서 대기 중',stopped:'정지됨',arrived:'도착 완료'};
async function request(url, options) {
  const response = await fetch(url, {...options, signal:AbortSignal.timeout(4000)});
  if (!response.ok) {
    const {detail} = await response.json().catch(() => ({}));
    const message = Array.isArray(detail) ? detail[0]?.msg : detail;
    throw new Error(message || `요청 실패 (${response.status})`);
  }
  return response.json();
}
function showResult(message, error = false) {
  const result = $('result');
  result.textContent = message;
  result.hidden = !message;
  result.classList.toggle('command-error', error);
}

let stopping = false;
let stopVersion = 0;

async function send(text) {
  if (stopping) return;
  const version = stopVersion;
  try {
    const result = await request('/api/commands', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    if (version !== stopVersion) return;
    const message = result.reason === 'unknown_intent'
      ? '명령을 이해하지 못했어요. 위에서 장소를 선택하거나 입력 예시처럼 요청해 주세요.'
      : result.message;
    showResult(message, !result.accepted);
    await refresh();
  } catch(error) {
    if (version === stopVersion) showResult('명령 전달 여부를 확인할 수 없습니다. 현재 주행 상태를 확인해 주세요.', true);
  }
}
$('command-form').addEventListener('submit', async event => {event.preventDefault();$('send').disabled=true;try{await send($('command').value);}finally{$('send').disabled=false;}});
$('stop').onclick = async () => {
  if (stopping) return;
  stopping = true;
  stopVersion++;
  $('stop').disabled = true;
  $('stop-label').textContent = '정지 요청 중';
  const feedback = $('stop-feedback');
  feedback.hidden = true;
  showResult('');
  try {
    const result = await request('/api/stop', {method:'POST'});
    if (!result.accepted) throw new Error(result.message);
    await refresh();
    feedback.textContent = '주행이 정지됐습니다.';
  } catch {
    feedback.textContent = '정지를 확인하지 못했습니다. 다시 눌러 주세요.';
  } finally {
    feedback.hidden = false;
    stopping = false;
    $('stop-label').textContent = '즉시 정지';
    $('stop').disabled = !currentMap;
  }
};
document.querySelectorAll('[data-command]').forEach(button => button.onclick=()=>{$('command').value=button.dataset.command;send(button.dataset.command);});
document.querySelectorAll('[data-condition]').forEach(button => button.onclick=async()=>{
  const conditions = {obstacle:button.dataset.condition==='obstacle',localization_valid:button.dataset.condition!=='localization'};
  try{
    await request('/api/mock/conditions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(conditions)});
    if (button.dataset.condition === 'clear') {
      showResult('모의 장애 조건을 해제했습니다. 목적지를 다시 선택해 주세요.');
    }
    await refresh();
  }
  catch(error){showResult(`시험 조건 변경 결과를 확인할 수 없습니다. ${error.message}`, true);}
});
async function replaceNavigationMap(register) {
  const buttons = ['choose-map', 'demo-map', 'maze-map', 'trial-map', 'register-map', 'start-demo'].map($);
  if (buttons[0].disabled) throw new Error('다른 주행 지도를 등록하는 중입니다. 잠시 뒤 다시 시도하세요.');
  buttons.forEach(button => button.disabled = true);
  try {
    const state = await register();
    $('map-result').textContent = `${state.navigation_map.frame_id} 지도를 등록했습니다. 로봇이 시작점에서 명령을 기다립니다.`;
    showResult('');
    $('demo-error').hidden = true;
    showNavigation(state);
    await refresh();
  } finally {
    buttons.forEach(button => button.disabled = false);
  }
}

async function loadMap(readMap) {
  if ($('choose-map').disabled) return false;
  try {
    await replaceNavigationMap(async () => request('/api/navigation/map', {
      method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(await readMap())
    }));
    return true;
  } catch(error) {
    $('map-result').textContent = `지도 등록 오류 · ${error.message}`;
    $('demo-error').textContent = '체험 지도를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.';
    $('demo-error').hidden = false;
    return false;
  }
}
$('choose-map').onclick = () => $('map-file').click();
$('map-file').onchange = () => loadMap(async () => {
  const file = $('map-file').files[0];
  $('map-file').value = '';
  if (!file) throw new Error('JSON 파일을 먼저 선택해 주세요.');
  return JSON.parse((await file.text()).replace(/^\uFEFF/, ''));
});
$('demo-map').onclick = () => loadMap(() => request('/static/navigation-demo.json'));
$('maze-map').onclick = () => loadMap(() => request('/static/navigation-maze.json', {cache:'no-store'}));
$('trial-map').onclick = () => loadMap(() => request('/static/navigation-preliminary.json'));
$('start-demo').onclick = $('demo-map').onclick;
const trialButtons = document.querySelectorAll('[data-trial-motion]');
let trialRequestPending = false;
for (const button of trialButtons) {
  button.onclick = async () => {
    if (trialRequestPending || stopping || !currentMapId) return;
    trialRequestPending = true;
    const version = stopVersion;
    for (const control of trialButtons) control.disabled = true;
    showResult('');
    try {
      const state = await request('/api/trial/motion', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({map_id:currentMapId, action:button.dataset.trialMotion}),
      });
      if (version === stopVersion && state.map_id === currentMapId) showNavigation(state);
    } catch (error) {
      if (version === stopVersion) showResult(error.message, true);
    } finally {
      trialRequestPending = false;
      await refresh();
    }
  };
}

let displayedNames = '';
function showDestinations(destinations, zones) {
  const names = Object.keys(destinations);
  $('place-hint').textContent = names.length
    ? '구역이나 장소를 누르면 바로 이동합니다.'
    : '등록된 장소가 없습니다. 지도 아래에서 장소나 구역을 추가할 수 있습니다.';
  const key = JSON.stringify([names, Object.keys(zones)]);
  if (displayedNames === key) return;
  displayedNames = key;
  $('command').placeholder = names.length ? `${names[0]}에 가줘` : '';
  $('command-hint').textContent = names.length > 1
    ? `예시 · ${names[0]}에 들렀다가 ${names[1]}에 가줘`
    : '이동 중에는 “멈춰”라고 요청할 수 있습니다.';
  const groups = [
    {label:'구역', className:'zone-group', names:names.filter(name => Object.hasOwn(zones, name))},
    {label:'장소', className:'point-group', names:names.filter(name => !Object.hasOwn(zones, name))},
  ];
  $('destinations').replaceChildren(...groups.filter(group => group.names.length).map(group => {
    const container = document.createElement('section');
    container.className = `destination-group ${group.className}`;
    const heading = document.createElement('h3');
    heading.textContent = group.label;
    const buttons = document.createElement('div');
    buttons.className = 'quick place-grid';
    for (const name of group.names) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = name;
      button.setAttribute('aria-label', `${name}에 가기 (${group.label})`);
      button.onclick = () => {
        $('command').value = `${name}에 가줘`;
        send($('command').value);
      };
      buttons.append(button);
    }
    container.append(heading, buttons);
    return container;
  }));
}

function gridLayout(canvas, grid) {
  const padding = 28;
  const size = Math.min((canvas.width - 2 * padding) / grid.cols, (canvas.height - 2 * padding) / grid.rows);
  return {
    size,
    left: (canvas.width - grid.cols * size) / 2,
    bottom: (canvas.height + grid.rows * size) / 2
  };
}

function gridCoordinatesAt(canvas, grid, clientX, clientY) {
  const bounds = canvas.getBoundingClientRect();
  const { size, left, bottom } = gridLayout(canvas, grid);
  const x = (clientX - bounds.left) * canvas.width / bounds.width;
  const y = (clientY - bounds.top) * canvas.height / bounds.height;
  return [(x - left) / size, (bottom - y) / size];
}

let lastMapState;
let mapFrame;
const navigationRaster = document.createElement('canvas');
let rasterMapId;
const navColors = {
  free: [40, 101, 85, 255], unknown: [24, 39, 52, 255],
  blocked: [168, 192, 201, 255], clearance: [96, 89, 133, 255],
};

new ResizeObserver(([entry]) => {
  const { width, height } = entry.contentRect;
  if (!width || !height) return;
  const canvas = $('map');
  const nextHeight = Math.round(canvas.width * height / width);
  if (canvas.height !== nextHeight) canvas.height = nextHeight;
  if (lastMapState) draw(lastMapState);
}).observe($('map'));

function renderNavigationRaster(mapId, grid) {
  if (mapId === rasterMapId) return;
  rasterMapId = mapId;
  const { cols, rows, data } = grid;
  navigationRaster.width = cols;
  navigationRaster.height = rows;
  const context = navigationRaster.getContext('2d');
  const image = context.createImageData(cols, rows);
  data.forEach((cell, index) => {
    const pixel = (rows - 1 - Math.floor(index / cols)) * cols + index % cols;
    const color = currentClearance.has(index) ? navColors.clearance : cell === 0 ? navColors.free
      : cell === -1 ? navColors.unknown : navColors.blocked;
    image.data.set(color, pixel * 4);
  });
  context.putImageData(image, 0, 0);
}

function renderMap(state) {
  cancelAnimationFrame(mapFrame);
  const previous = lastMapState;
  lastMapState = state;
  const next = state.route[0];
  const previousNext = previous?.route[0];
  const sameSegment = next && previousNext
    ? next[0] === previousNext[0] && next[1] === previousNext[1]
    : !next && !previousNext;
  if (state.status !== 'moving' || previous?.status !== 'moving' ||
      previous.map_id !== state.map_id || !sameSegment) {
    draw(state);
    return;
  }
  const start = performance.now();
  const headingChange = Math.atan2(Math.sin(state.heading_rad - previous.heading_rad),
    Math.cos(state.heading_rad - previous.heading_rad));
  function animate(now) {
    const progress = Math.min(1, (now - start) / 100);
    const position = state.position.map((value, axis) =>
      previous.position[axis] + (value - previous.position[axis]) * progress);
    draw({...state, position, heading_rad:previous.heading_rad + headingChange * progress});
    if (progress < 1) mapFrame = requestAnimationFrame(animate);
  }
  mapFrame = requestAnimationFrame(animate);
}

function draw(state) {
  const canvas=$('map'),ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;
  const grid = state.navigation_map;
  const heading = grid && state.localization !== 'lost' ? agvPose.heading(state.heading_rad) : '—';
  if ($('nav-heading').textContent !== heading) $('nav-heading').textContent = heading;
  ctx.clearRect(0,0,w,h);
  if (!grid) {
    initialPoseMarker.hidden = true;
    ctx.fillStyle='#97a7bf';ctx.font='28px sans-serif';ctx.textAlign='center';
    ctx.fillText('지도를 만들거나 불러와 주세요', w / 2, h / 2);
    ctx.textAlign='start';
    return;
  }
  const {cols, rows, resolution_m: resolution, origin_x_m, origin_y_m} = grid;
  const {size, left, bottom} = gridLayout(canvas, grid);
  const point = ([x, y]) => [left + (x - origin_x_m) / resolution * size,
                             bottom - (y - origin_y_m) / resolution * size];
  renderInitialPoseMarker(grid, point, w, h);
  renderNavigationRaster(state.map_id, grid);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(navigationRaster, left, bottom - rows * size, cols * size, rows * size);
  ctx.strokeStyle='#263e4b';ctx.lineWidth=1;
  const zoneColors = ['#3779a4', '#377c68', '#785a93', '#9b6750', '#6963a0', '#487b82'];
  const zones = Object.entries(grid.zones ?? {});
  zones.forEach(([, cells], index) => {
    ctx.fillStyle = zoneColors[index % zoneColors.length];
    for (const [col, row] of cells) {
      ctx.fillRect(left + col * size, bottom - (row + 1) * size, size, size);
    }
  });
  if (size >= 8) {
    for(let x=0;x<=cols;x++){ctx.beginPath();ctx.moveTo(left+x*size,bottom-rows*size);ctx.lineTo(left+x*size,bottom);ctx.stroke();}
    for(let y=0;y<=rows;y++){ctx.beginPath();ctx.moveTo(left,bottom-y*size);ctx.lineTo(left+cols*size,bottom-y*size);ctx.stroke();}
  }
  if (!$('destination-form').hidden) {
    ctx.fillStyle = '#efc66c88';
    for (const [col, row] of zoneCells) {
      ctx.fillRect(left + col * size, bottom - (row + 1) * size, size, size);
    }
    const [col, row] = selectedCell();
    if (isFreeCell(grid, col, row)) {
      ctx.fillStyle = '#efc66c55';
      ctx.fillRect(left + col * size, bottom - (row + 1) * size, size, size);
    }
  }
  if (initialPoseTools.open && !initialPoseFields.disabled) {
    const position = initialPosePosition();
    const [col, row] = mapCellAt(grid, position);
    if (isFreeCell(grid, col, row)) {
      const cellX = left + col * size;
      const cellY = bottom - (row + 1) * size;
      ctx.strokeStyle = '#ffe39c';
      ctx.lineWidth = 2;
      ctx.strokeRect(cellX, cellY, size, size);
    }
  }
  for (const [name, position] of Object.entries(state.destinations)) {
    const [x, y] = point(position);
    const isZone = Object.hasOwn(grid.zones ?? {}, name);
    ctx.beginPath();
    ctx.arc(x, y, 11, 0, Math.PI * 2);
    if (isZone) {
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 4;
      ctx.stroke();
    } else {
      ctx.fillStyle = '#63dbb5';
      ctx.fill();
    }
    ctx.font = 'bold 22px sans-serif';
    const labelWidth = ctx.measureText(name).width;
    const labelX = x + 19 + labelWidth <= w ? x + 19 : Math.max(4, x - 19 - labelWidth);
    ctx.fillStyle = isZone ? '#fff' : '#63dbb5';
    ctx.lineWidth = 4;
    ctx.strokeStyle = '#101c30';
    ctx.strokeText(name, labelX, Math.min(y + 7, h - 4), w - labelX - 4);
    ctx.fillText(name, labelX, Math.min(y + 7, h - 4), w - labelX - 4);
  }
  if(state.localization==='lost'){ctx.fillStyle='#f3c36a';ctx.font='22px sans-serif';ctx.fillText('위치 추정 실패 · 로봇 위치 표시 중단',30,50);return;}
  const robot=point(state.position);
  if (state.target) {
    ctx.strokeStyle='#8fcaff';ctx.lineWidth=4;ctx.beginPath();ctx.moveTo(...robot);
    for (const waypoint of state.route) ctx.lineTo(...point(waypoint));
    ctx.stroke();
  }
  const [x, y] = robot;
  agvPose.draw(ctx, x, y, size / resolution, state.heading_rad);

  const label = 'AGV-01 · ' + heading;
  ctx.font = 'bold 24px sans-serif';
  const labelWidth = ctx.measureText(label).width + 24;
  const labelX = Math.max(4, Math.min(x - labelWidth / 2, w - labelWidth - 4));
  const labelY = y >= 66 ? y - 64 : y + 29;
  ctx.fillStyle = '#101c30';
  ctx.fillRect(labelX, labelY, labelWidth, 34);
  ctx.fillStyle = '#e5efff';
  ctx.fillText(label, labelX + 12, labelY + 25);
}
let currentMap = null;
let currentMapId = null;
let currentClearance = new Set();
const destinationForm = $('destination-form');
const destinationResult = $('destination-result');
const destinationCol = $('destination-col');
const destinationRow = $('destination-row');
const destinationType = $('destination-type');
const initialPoseTools = $('initial-pose-tools');
const initialPoseForm = $('initial-pose-form');
const initialPoseFields = initialPoseForm.querySelector('fieldset');
const initialPoseX = $('initial-pose-x');
const initialPoseY = $('initial-pose-y');
const initialPoseHeading = $('initial-pose-heading');
const initialPoseStatus = $('initial-pose-status');
const initialPoseMarker = $('initial-pose-marker');
const initialDirectionButtons = document.querySelectorAll('[data-initial-heading]');
let initialPoseOnline = false;
let initialPoseMoving = false;
let initialPosePending = false;
let initialPoseDrag;
let zoneCells = [];
const removePlaceForm = $('remove-place-form');
const removePlaceSelect = $('remove-place');
const removePlaceButton = $('remove-place-button');
const removePlaceResult = $('remove-place-result');
let displayedPlaces = '';
let removingPlace = false;

function showPlaceManagement(grid) {
  const places = Object.keys(grid?.destinations ?? {}).map(name => [name, Boolean(grid.zones?.[name])]);
  const key = JSON.stringify([currentMapId, places]);
  if (displayedPlaces === key) return;
  displayedPlaces = key;
  const previous = removePlaceSelect.value;
  removePlaceSelect.replaceChildren(
    new Option('선택해 주세요', ''),
    ...places.map(([name, isZone]) => new Option(isZone ? `${name} (구역)` : name, name)),
  );
  if (places.some(([name]) => name === previous)) removePlaceSelect.value = previous;
  removePlaceButton.disabled = removingPlace || !removePlaceSelect.value;
  removePlaceForm.hidden = !places.length;
}

removePlaceSelect.onchange = () => {
  removePlaceButton.disabled = removingPlace || !removePlaceSelect.value;
  removePlaceResult.textContent = '';
};
removePlaceForm.onsubmit = async event => {
  event.preventDefault();
  const name = removePlaceSelect.value;
  const mapId = currentMapId;
  if (!name || !mapId || removingPlace) return;
  removingPlace = true;
  removePlaceButton.disabled = true;
  try {
    const state = await request(
      `/api/navigation/destinations/${encodeURIComponent(name)}?map_id=${encodeURIComponent(mapId)}`,
      {method:'DELETE'},
    );
    await refresh();
    if (currentMapId === mapId) {
      const stopped = state.events[0]?.reason === 'destination_removed';
      removePlaceResult.textContent = stopped
        ? `‘${name}’을(를) 삭제하고 주행을 정지했습니다.`
        : `‘${name}’을(를) 삭제했습니다.`;
    }
  } catch (error) {
    removePlaceResult.textContent = error.message;
  } finally {
    removingPlace = false;
    removePlaceButton.disabled = !removePlaceSelect.value;
  }
};

function describeSelection() {
  const zone = destinationType.value === 'zone';
  $('toggle-zone-cell').hidden = !zone;
  destinationCol.required = destinationRow.required = !zone;
  destinationResult.textContent = zone
    ? `선택한 칸 ${zoneCells.length}개. 지도에서 통행 가능한 칸을 두 개 이상 선택하세요. 새 구역은 첫 번째 칸이 도착 지점입니다.`
    : '지도에서 빈 칸을 클릭하거나 열과 행을 입력하세요. 왼쪽 아래가 [0, 0]입니다.';
}

function toggleZoneCell(col, row) {
  if (!currentMap || !isFreeCell(currentMap, col, row)) {
    destinationResult.textContent = '지도 안의 통행 가능한 칸을 선택해 주세요.';
    return;
  }
  const index = zoneCells.findIndex(([x, y]) => x === col && y === row);
  if (index < 0) zoneCells.push([col, row]);
  else zoneCells.splice(index, 1);
  describeSelection();
}

destinationType.onchange = () => {
  zoneCells = [];
  describeSelection();
};
$('toggle-zone-cell').onclick = () => toggleZoneCell(...selectedCell());
$('place-tools').addEventListener('toggle', event => {
  if (event.currentTarget.open) initialPoseTools.open = false;
  else showDestinationForm(false);
});

function mapCellAt(grid, [x, y]) {
  return [Math.floor((x - grid.origin_x_m) / grid.resolution_m),
    Math.floor((y - grid.origin_y_m) / grid.resolution_m)];
}

function mapPositionAt(grid, col, row) {
  return [grid.origin_x_m + col * grid.resolution_m, grid.origin_y_m + row * grid.resolution_m];
}

function positionLabel([x, y]) {
  return 'X ' + x + ' m, Y ' + y + ' m';
}

function initialPosePosition() {
  return [initialPoseX.valueAsNumber, initialPoseY.valueAsNumber];
}

function renderInitialPoseMarker(grid, project, width, height) {
  const position = initialPosePosition();
  const [x, y] = project(position);
  const visible = initialPoseTools.open && !initialPoseFields.disabled && Number.isFinite(x) && Number.isFinite(y)
    && (initialPoseDrag || (x >= 0 && x <= width && y >= 0 && y <= height));
  initialPoseMarker.hidden = !visible;
  if (!visible) return;
  initialPoseMarker.style.left = (x / width * 100) + '%';
  initialPoseMarker.style.top = (y / height * 100) + '%';
  initialPoseMarker.classList.toggle('is-blocked', !isFreeCell(grid, ...mapCellAt(grid, position)));
}

function setPoseDraft(position) {
  [initialPoseX.value, initialPoseY.value] = position;
  redrawInitialPose();
}

function finishPoseDrag(cancelled = false) {
  const drag = initialPoseDrag;
  if (!drag) return;
  initialPoseDrag = undefined;
  if (initialPoseMarker.hasPointerCapture(drag.id)) initialPoseMarker.releasePointerCapture(drag.id);
  initialPoseMarker.classList.remove('is-dragging');
  if (!drag.moved || drag.mapId !== currentMapId) return;
  const position = initialPosePosition();
  if (cancelled || !isFreeCell(currentMap, ...mapCellAt(currentMap, position))) {
    setPoseDraft(drag.original);
    initialPoseStatus.textContent = cancelled ? '' : '이 위치에서는 통행할 수 없습니다. 원래 위치로 되돌렸습니다.';
    return;
  }
  initialPoseStatus.textContent = '옮긴 시작점 ' + positionLabel(position) + '. 초기 상태 적용을 누르세요.';
  redrawInitialPose();
}

function movePoseMarker(event) {
  const drag = initialPoseDrag;
  if (drag?.id !== event.pointerId) return;
  if (!drag.moved && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 4) return;
  drag.moved = true;
  const [gridX, gridY] = gridCoordinatesAt($('map'), currentMap, event.clientX - drag.dx, event.clientY - drag.dy);
  setPoseDraft(mapPositionAt(currentMap, gridX, gridY).map(value => Number(value.toFixed(4))));
}

initialPoseMarker.onpointerdown = event => {
  if (event.button !== 0 || initialPoseFields.disabled || initialPoseDrag) return;
  const rect = initialPoseMarker.getBoundingClientRect();
  initialPoseDrag = {
    id: event.pointerId, mapId: currentMapId, original: [initialPoseX.value, initialPoseY.value],
    x: event.clientX, y: event.clientY,
    dx: event.clientX - (rect.left + rect.width / 2),
    dy: event.clientY - (rect.top + rect.height / 2),
    moved: false,
  };
  initialPoseMarker.setPointerCapture(event.pointerId);
  initialPoseMarker.classList.add('is-dragging');
};
initialPoseMarker.onpointermove = movePoseMarker;
initialPoseMarker.onpointerup = event => {
  if (initialPoseDrag?.id !== event.pointerId) return;
  movePoseMarker(event);
  finishPoseDrag();
};
initialPoseMarker.onpointercancel = initialPoseMarker.onlostpointercapture = event => {
  if (initialPoseDrag?.id === event.pointerId) finishPoseDrag(true);
};
initialPoseMarker.onkeydown = event => {
  const step = { ArrowLeft: [-.01, 0], ArrowRight: [.01, 0], ArrowDown: [0, -.01], ArrowUp: [0, .01] }[event.key];
  if (!step || initialPoseFields.disabled) return;
  event.preventDefault();
  const position = initialPosePosition().map((value, axis) => Number((value + step[axis]).toFixed(4)));
  if (!isFreeCell(currentMap, ...mapCellAt(currentMap, position))) {
    initialPoseStatus.textContent = '이 위치에서는 통행할 수 없습니다.';
    return;
  }
  setPoseDraft(position);
  initialPoseStatus.textContent = '옮긴 시작점 ' + positionLabel(position) + '. 초기 상태 적용을 누르세요.';
};

function showInitialDirection() {
  for (const button of initialDirectionButtons) {
    button.setAttribute('aria-pressed', String(Number(button.dataset.initialHeading) === initialPoseHeading.valueAsNumber));
  }
}

function fillInitialPose() {
  const position = currentMap?.start_position_m ?? (currentMap
    ? mapPositionAt(currentMap, currentMap.start[0] + .5, currentMap.start[1] + .5)
      .map(value => Number(value.toFixed(10)))
    : ['', '']);
  [initialPoseX.value, initialPoseY.value] = position;
  initialPoseHeading.value = currentMap?.start_heading_deg ?? 90;
  showInitialDirection();
  initialPoseStatus.textContent = '';
}

function updateInitialPoseControls() {
  initialPoseFields.disabled = !initialPoseOnline || !currentMap || initialPoseMoving || initialPosePending;
  $('initial-pose-availability').textContent = !initialPoseOnline ? '서버 연결을 확인해 주세요.'
    : !currentMap ? '시작 위치를 지정하려면 먼저 주행 지도를 불러오세요.'
      : initialPoseMoving ? '주행을 정지하거나 완료한 뒤 지정할 수 있습니다.' : '';
  $('map').classList.toggle('selecting-initial-pose', initialPoseTools.open && !initialPoseFields.disabled);
  if (!initialPoseTools.open || initialPoseFields.disabled) {
    finishPoseDrag(true);
    initialPoseMarker.hidden = true;
  }
}

function redrawInitialPose() {
  if (lastMapState) draw(lastMapState);
}

initialPoseTools.addEventListener('toggle', () => {
  if (initialPoseTools.open) {
    $('place-tools').open = false;
    showDestinationForm(false);
    fillInitialPose();
  }
  updateInitialPoseControls();
  redrawInitialPose();
});
initialPoseX.oninput = initialPoseY.oninput = redrawInitialPose;
initialPoseHeading.oninput = showInitialDirection;
initialDirectionButtons.forEach(button => {
  button.onclick = () => {
    initialPoseHeading.value = button.dataset.initialHeading;
    showInitialDirection();
  };
});
initialPoseForm.onsubmit = async event => {
  event.preventDefault();
  if (initialPoseFields.disabled) return;
  const position_m = initialPosePosition();
  if (!isFreeCell(currentMap, ...mapCellAt(currentMap, position_m))) {
    initialPoseStatus.textContent = '로봇이 통행하고 회전할 수 있는 좌표를 선택해 주세요.';
    return;
  }
  const mapId = currentMapId;
  const heading_deg = initialPoseHeading.valueAsNumber;
  initialPosePending = true;
  updateInitialPoseControls();
  try {
    const state = await request('/api/navigation/initial-pose', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ map_id: mapId, position_m, heading_deg }),
    });
    if (mapId !== currentMapId) return;
    showNavigation(state);
    $('stop-feedback').hidden = true;
    showResult('');
    initialPoseStatus.textContent = '초기 위치 ' + positionLabel(position_m) + ' · 방향 ' + heading_deg + '°를 적용했습니다.';
  } catch (error) {
    if (mapId === currentMapId) initialPoseStatus.textContent = error.message;
  } finally {
    initialPosePending = false;
    updateInitialPoseControls();
    redrawInitialPose();
  }
};

function selectedCell() {
  return [destinationCol.valueAsNumber, destinationRow.valueAsNumber];
}

function isFreeCell(grid, col, row) {
  const index = row * grid.cols + col;
  return Number.isInteger(col) && Number.isInteger(row) &&
    col >= 0 && col < grid.cols && row >= 0 && row < grid.rows &&
    grid.data[index] === 0 && !currentClearance.has(index);
}

function showDestinationForm(open) {
  destinationForm.hidden = !open;
  $('add-destination').setAttribute('aria-expanded', String(open));
  $('map').classList.toggle('selecting-destination', open);
  if (open) describeSelection();
  else zoneCells = [];
}

$('add-destination').onclick = () => {
  showDestinationForm(destinationForm.hidden);
  if (destinationForm.hidden) destinationResult.textContent = '';
};

$('map').onclick = event => {
  if (!currentMap || (destinationForm.hidden && (!initialPoseTools.open || initialPoseFields.disabled))) return;
  const [gridX, gridY] = gridCoordinatesAt(event.currentTarget, currentMap, event.clientX, event.clientY);
  const col = Math.floor(gridX);
  const row = Math.floor(gridY);
  if (initialPoseTools.open && !initialPoseFields.disabled) {
    const position = mapPositionAt(currentMap, gridX, gridY).map(value => Number(value.toFixed(4)));
    if (!isFreeCell(currentMap, ...mapCellAt(currentMap, position))) {
      initialPoseStatus.textContent = '로봇이 통행하고 회전할 수 있는 좌표를 선택해 주세요.';
      return;
    }
    setPoseDraft(position);
    initialPoseStatus.textContent = '선택한 초기 위치 ' + positionLabel(position) + '. 적용 버튼을 누르세요.';
    return;
  }
  if (destinationType.value === 'zone') {
    toggleZoneCell(col, row);
    return;
  }
  if (!isFreeCell(currentMap, col, row)) {
    destinationCol.value = destinationRow.value = '';
    destinationResult.textContent = '지도 안의 통행 가능한 칸을 선택해 주세요.';
    return;
  }
  destinationCol.value = col;
  destinationRow.value = row;
  destinationResult.textContent = `[${col}, ${row}] 칸을 선택했습니다. 이름을 입력하고 등록하세요.`;
  $('destination-name').focus();
};

destinationForm.onsubmit = async event => {
  event.preventDefault();
  const isZone = destinationType.value === 'zone';
  if (isZone && zoneCells.length < 2) {
    destinationResult.textContent = '구역에 포함할 칸을 두 개 이상 선택해 주세요.';
    return;
  }
  const cell = isZone ? zoneCells[0] : selectedCell();
  const name = $('destination-name').value.trim().replace(/\s+/g, ' ');
  if (!currentMap || !isFreeCell(currentMap, ...cell)) {
    destinationResult.textContent = '지도 안의 통행 가능한 칸을 선택해 주세요.';
    return;
  }
  if (!/^[A-Za-z0-9가-힣]+(?: [A-Za-z0-9가-힣]+)*$/.test(name)) {
    destinationResult.textContent = '이름에는 한글, 영문, 숫자, 공백을 사용할 수 있습니다.';
    return;
  }
  const mapId = currentMapId;
  $('save-destination').disabled = true;
  try {
    await request('/api/navigation/destinations', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({map_id:mapId, name, cell, ...(isZone && {zone_cells:zoneCells})})
    });
    await refresh();
    if (currentMapId === mapId) {
      destinationForm.reset();
      showDestinationForm(false);
      destinationResult.textContent = `'${name}'을(를) 등록했습니다. “${name}에 가줘”라고 입력하거나 목적지 버튼을 누르세요.`;
    }
  } catch(error) {
    destinationResult.textContent = error.message;
  } finally {
    $('save-destination').disabled = false;
  }
};

function showNavigation(state) {
  currentMap = state.navigation_map;
  if (currentMapId !== state.map_id) {
    currentMapId = state.map_id;
    finishPoseDrag(true);
    currentClearance = new Set(state.footprint_blocked_cells ?? []);
    destinationForm.reset();
    showDestinationForm(false);
    destinationResult.textContent = '';
    removePlaceResult.textContent = '';
    fillInitialPose();
  }
  $('add-destination').disabled = !currentMap;
  if (currentMap) {
    destinationCol.max = currentMap.cols - 1;
    destinationRow.max = currentMap.rows - 1;
  }
  $('command-controls').disabled = $('download-map').disabled = !currentMap;
  $('stop').disabled = stopping || !currentMap;
  $('command-controls').hidden = !currentMap;
  $('demo-start').hidden = Boolean(currentMap);
  $('map-frame').textContent = currentMap?.frame_id ?? '지도 등록 대기';
  $('map-legend').textContent = currentMap
    ? '보라색은 20 × 20cm 로봇의 여유 공간 차단 영역입니다. 흰 링은 구역 도착 지점, 파란 선은 남은 경로입니다. 방향각은 위쪽 0°에서 시계방향으로 표시합니다.'
    : '지도를 등록하면 로봇이 시작점에 표시됩니다.';
  const block = currentMap && state.explanation.active_block;
  $('map-block').hidden = !block;
  $('map-block-message').textContent = block === 'localization_lost'
    ? '모의 위치 추정 실패로 로봇 위치 표시와 이동이 중단됐습니다.'
    : block === 'obstacle' ? '모의 장애물 시험이 켜져 있어 이동이 차단됐습니다.' : '';
  const trial = currentMap?.frame_id === 'preliminary-1m';
  let status = currentMap ? labels[state.status] : '지도 준비 필요';
  if (trial && state.status === 'moving') status = state.route.length ? '직진 중' : '제자리 회전 중';
  if (trial && state.status === 'arrived') status = '동작 완료';
  if ($('status').textContent !== status) $('status').textContent = status;
  const arrived = state.status === 'arrived' && state.events.find(event => event.type === 'arrived');
  const arrivedName = arrived?.destination;
  $('active-destination').textContent = state.target ||
    (arrivedName && (trial || Object.hasOwn(state.destinations, arrivedName)) ? arrivedName : '');
  $('position').textContent = state.position?.map(value => value.toFixed(3)).join(' , ') ?? '—';
  if (state.localization === 'lost') $('position').textContent = '위치 추정 실패';
  if (state.status === 'moving' || state.status === 'waiting') $('stop-feedback').hidden = true;
  showDestinations(state.destinations, currentMap?.zones ?? {});
  $('place-hint').hidden = $('destinations').hidden = $('command-form').hidden = trial;
  $('drive-title').textContent = trial ? '예비 주행 조작' : '어디로 이동할까요?';
  $('trial-controls').hidden = !trial;
  const driving = state.status === 'moving' || state.status === 'waiting';
  initialPoseMoving = driving;
  updateInitialPoseControls();
  for (const button of trialButtons) button.disabled = driving || stopping || trialRequestPending || Boolean(block);
  showPlaceManagement(currentMap);
  renderMap(state);
}
$('download-map').onclick = () => {
  if (!currentMap) return;
  const link = document.createElement('a');
  const url = URL.createObjectURL(new Blob([JSON.stringify(currentMap, null, 2)], {type:'application/json'}));
  link.href = url;
  link.download = 'navigation-map.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
function showConnection(online) {
  initialPoseOnline = online;
  const label = $('operator-server-status');
  label.textContent = online ? '서버 연결됨' : '서버 연결 끊김';
  label.parentElement.classList.toggle('online', online);
  $('connection').textContent = online ? '연결됨' : '연결 끊김';
  if (!online) {
    $('nav-heading').textContent = '—';
    updateInitialPoseControls();
  }
}

function openLinkedMapSettings() {
  if (window.location.hash === '#destinations-settings') $('place-tools').open = true;
  if (window.location.hash === '#initial-pose-tools') initialPoseTools.open = true;
}
window.addEventListener('hashchange', openLinkedMapSettings);
openLinkedMapSettings();

async function refresh(){
  try {
    const state=await request('/api/state');showConnection(true);showNavigation(state);
    $('explanation').textContent=state.explanation.message;
    $('evidence').textContent=`상태·이벤트 기반 설명 · 근거 #${state.explanation.evidence_event_id} · 규칙 기반 파서`;
    $('conditions').textContent=`모의 장애물: ${state.conditions.obstacle?'있음':'없음'} · 모의 위치 추정: ${state.conditions.localization_valid?'정상':'실패'}`;
    $('events').replaceChildren(...state.events.map(event=>{const li=document.createElement('li');li.textContent=`#${event.id} ${event.message}`;const meta=document.createElement('small');meta.textContent=`${new Date(event.timestamp).toLocaleTimeString('ko-KR')} · ${event.type}${event.reason?' · '+event.reason:''}`;li.append(meta);return li;}));
    $('history').replaceChildren(...state.history.map(entry=>{const li=document.createElement('li');li.textContent=entry.text;const meta=document.createElement('small');meta.textContent=`${new Date(entry.timestamp).toLocaleTimeString('ko-KR')} · ${entry.intent} · ${entry.accepted?'수락':'거부'}`;li.append(meta);return li;}));
  }catch(error){cancelAnimationFrame(mapFrame);lastMapState=undefined;$('command-controls').disabled=true;showConnection(false);$('status').textContent='연결이 끊겼습니다';$('active-destination').textContent='';$('position').textContent='—';$('explanation').textContent='백엔드 연결이 끊겨 현재 주행 상태를 확인할 수 없습니다.';$('evidence').textContent='기존 이벤트는 마지막 수신 기록입니다.';$('conditions').textContent='현재 시험 조건 확인 불가';const canvas=$('map');canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);}
}
async function poll(){await refresh();setTimeout(poll,lastMapState?.status === 'moving' ? 100 : 500);}poll();
