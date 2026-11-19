"""Run on the JetBot from /workspace with Python 3.6 or later."""
import argparse
import heapq
import json
import math
import re
import secrets
import signal
import threading
import time
import unicodedata
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


PORT = 8011
TARGET_FORWARD_CM = 10
MEASURED_FORWARD_CM = 9
TARGET_TURN_DEGREES = 90
MEASURED_TURN_DEGREES = 80
PREVIOUS_TURN_SECONDS = 2.95
SPACE_CM = 100
ROBOT_SIZE_CM = 20
ROUTE_LINK_TIMEOUT_S = 2
BODY_RADIUS_CM = ROBOT_SIZE_CM / math.sqrt(2)
OBSTACLE_MARGIN_CM = 2
NEAR_GOAL_LIMIT_CM = 8
NEAR_GOAL_WALL_MARGIN_CM = 2
MAX_OBSTACLES = 12
CM_UNIT = r'(?:cm|센티(?:미터)?|센치)'
FORWARD_DISTANCE = rf'(?:한\s+|약\s*)?(\d{{1,3}})\s*{CM_UNIT}\s*(?:만\s*)?'
POLITE_ENDING = r'\s*(?:줘(?:요)?|주세요|요)?'
TURN_REQUEST = rf'\s*(?:90\s*(?:도|°)\s*)?(?:회전(?:해)?|돌아|돌려){POLITE_ENDING}'
MAP_HEADINGS = {'오른쪽': 0, '오른쪽 위': 45, '위쪽': 90, '왼쪽 위': 135, '왼쪽': 180,
                '왼쪽 아래': 225, '아래쪽': 270, '오른쪽 아래': 315}
DIRECT_SETUP_ACTIONS = ('anchor', 'reset-position', 'reset-start', 'route-clear')
COORDINATES = rf'(?:좌표\s*)?\(?\s*(\d{{1,3}})\s*{CM_UNIT}?\s*,\s*(\d{{1,3}})\s*{CM_UNIT}?\s*\)?'
POINT_REQUEST = re.compile(
    rf'(?P<name>[가-힣A-Za-z0-9 ]{{1,24}}?)(?:\s*지점)?(?:을|를)?\s+{COORDINATES}'
    rf'\s*(?:에|으로|로)?\s*(?:등록|설정)(?:해)?{POLITE_ENDING}',
    re.IGNORECASE,
)
CORNER_REQUEST = re.compile(
    rf'(?:지도\s*)?(왼쪽|오른쪽)\s*(앞|뒤)에?\s+([가-힣A-Za-z0-9 ]{{1,24}}?)(?:\s*지점)?(?:을|를)?'
    rf'\s+(?:등록|설정)(?:해)?{POLITE_ENDING}',
    re.IGNORECASE,
)
COMMAND_PATTERNS = (
    ('stop', r'(?:지금\s*|바로\s*|즉시\s*)*(?:멈춰|(?:주행\s*)?(?:정지|취소)(?:\s*해)?)\s*(?:줘(?:요)?|주세요|요)?'),
    ('distance', rf'(?:직진|전진)\s*거리(?:를)?\s*{FORWARD_DISTANCE}(?:로|으로)?\s*(?:설정|변경|바꿔)(?:해)?{POLITE_ENDING}'),
    ('forward', rf'(?:앞으로\s*)?{FORWARD_DISTANCE}(?:가|이동(?:해)?|직진(?:해)?){POLITE_ENDING}'),
    ('forward', rf'앞으로\s*(?:가|이동(?:해)?|직진(?:해)?){POLITE_ENDING}'),
    ('forward', rf'(?:직진|전진)(?:해|\s*{FORWARD_DISTANCE})?{POLITE_ENDING}'),
    ('backward', rf'뒤로\s*(?:{FORWARD_DISTANCE})?(?:가|이동(?:해)?|후진(?:해)?){POLITE_ENDING}'),
    ('backward', rf'후진(?:해|\s*{FORWARD_DISTANCE})?{POLITE_ENDING}'),
    ('left', rf'(?:왼쪽(?:으로)?|좌로){TURN_REQUEST}|좌회전(?:해)?{POLITE_ENDING}'),
    ('right', rf'(?:오른쪽(?:으로)?|우로){TURN_REQUEST}|우회전(?:해)?{POLITE_ENDING}'),
)


def parse_command(text, point_names=()):
    text = ' '.join(unicodedata.normalize('NFKC', text).split()).rstrip('.!?').strip()
    text = re.sub(r'^(?:젯봇[아이]?|jetbot|로봇아)[, ]+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:지금|바로|이제|일단)\s+', '', text)
    text = re.sub(r'\s*(?:해\s*)?줄래(?:요)?$', '줘', text)
    text = re.sub(r'가\s*볼래(?:요)?$', '가줘', text)
    text = re.sub(r'\s*좀\s+', ' ', text)
    korean_distances = {'열': 10, '십': 10, '열다섯': 15, '십오': 15, '스무': 20, '스물': 20,
                        '이십': 20, '삼십': 30, '서른': 30, '사십': 40, '마흔': 40}
    text = re.sub(rf'(?<![가-힣0-9])({"|".join(korean_distances)})\s*(?={CM_UNIT})',
                  lambda match: str(korean_distances[match[1]]), text)
    reset = re.fullmatch(
        rf'(시작\s*점|(?:로봇|현재)\s*위치)(?:을|를)?\s*초기화(?:해)?{POLITE_ENDING}', text,
    )
    if reset:
        return ('reset-start' if reset.group(1).replace(' ', '') == '시작점' else 'reset-position'), None
    if re.fullmatch(rf'현재\s*위치(?:를)?\s*시작\s*점(?:으로)?\s*(?:설정|맞춰)(?:해)?{POLITE_ENDING}', text):
        return 'anchor', None
    point = POINT_REQUEST.fullmatch(text)
    if point:
        name, x, y = point.groups()
        name = ' '.join(name.split())
        return 'position' if name in ('현재 위치', '현재위치') else 'point', (name, int(x), int(y))
    heading = re.fullmatch(
        rf'(?:현재\s*)?(?:바라보는\s*)?방향(?:을)?\s*({"|".join(MAP_HEADINGS)}|\d{{1,3}}\s*(?:도|°))'
        rf'(?:으로|로)?\s*(?:(?:설정|변경)(?:해)?|맞춰|바꿔){POLITE_ENDING}',
        text,
    )
    if heading:
        direction = heading[1]
        return 'heading', MAP_HEADINGS.get(direction) if direction in MAP_HEADINGS else int(direction[:-1])
    corner = CORNER_REQUEST.fullmatch(text)
    if corner:
        side, depth, name = corner.groups()
        x = 25 if side == '왼쪽' else 75
        y = 75 if depth == '앞' else 25
        return 'point', (' '.join(name.split()), x, y)
    for action, pattern in COMMAND_PATTERNS:
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1) if match.lastindex else None
            return action, int(value) if value else None
    distance_first = re.fullmatch(
        rf'{FORWARD_DISTANCE}(앞으로|뒤로|앞쪽으로|뒤쪽으로)\s*(?:가|이동(?:해)?|움직여|빼){POLITE_ENDING}',
        text, re.IGNORECASE,
    )
    if distance_first:
        return ('forward' if distance_first[2].startswith('앞') else 'backward'), int(distance_first[1])
    colloquial = re.fullmatch(
        rf'(앞(?:쪽)?으로|뒤로|뒤쪽으로)\s*(?:{FORWARD_DISTANCE})?(?:움직여|빼){POLITE_ENDING}',
        text, re.IGNORECASE,
    )
    if colloquial:
        return ('forward' if colloquial[1].startswith('앞') else 'backward'), (
            int(colloquial[2]) if colloquial[2] else None)
    targets = []
    quote = r"""["']?"""
    for name in point_names:
        flexible_name = r'\s*'.join(map(re.escape, name.replace(' ', '')))
        if re.fullmatch(
            rf'{quote}{flexible_name}{quote}\s*(?:지점\s*)?(?:으로|로|에|까지|쪽으로|쪽에)?\s*'
            rf'(?:가|가봐|이동(?:해)?|보내|주행(?:해)?){POLITE_ENDING}', text, re.IGNORECASE,
        ):
            targets.append(name)
    if len(targets) == 1:
        return 'navigate', targets[0]
    return None, None


PAGE = """<!doctype html>
<html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JetBot 예비 주행</title>
<style>
  body {font: 17px system-ui,sans-serif; background:#101827; color:#edf3ff; margin:2rem auto; padding:0 1rem; max-width:68rem}
  main {display:grid; grid-template-columns:minmax(0, 1.1fr) minmax(0, .9fr); grid-template-areas:"map control"; gap:1.5rem; align-items:start}
  #control-panel {grid-area:control}
  #map-panel {grid-area:map}
  section {min-width:0; padding:1.25rem; border:1px solid #354961; border-radius:.75rem; background:#141f30}
  h2 {margin:0 0 .75rem; font-size:1.3rem}
  @media (max-width:760px) {main {grid-template-columns:minmax(0,1fr); grid-template-areas:"control" "map"}}
  button {font:inherit; padding:0.8rem; border:0; border-radius:.5rem; color:white; background:#285ba4; margin:.25rem}
  button:disabled {opacity:.45}
  #stop {background:#b12b43}
  form {margin-top:1.5rem}
  label {display:block; margin-bottom:.4rem}
  input {font:inherit; padding:.65rem; border:1px solid #526581; border-radius:.5rem; color:inherit; background:#17243a; width:min(100%, 28rem); box-sizing:border-box}
  .help {color:#bdcde7; font-size:.9rem}
  #workspace {position:relative; width:min(100%, 32rem); aspect-ratio:1; margin:1rem 0; border:1px solid #89a2bf; background-color:#17243a;
    background-image:linear-gradient(to right,#2e425b 1px,transparent 1px),linear-gradient(to top,#2e425b 1px,transparent 1px); background-size:10% 10%}
  #workspace[data-pick] {cursor:crosshair; outline:2px solid #82f0be}
  #goal-form {margin:.6rem 0}
  #robot-map {position:absolute; inset:0; width:100%; height:100%; pointer-events:none}
  .map-key {display:flex; flex-wrap:wrap; gap:.35rem 1rem; font-size:.9rem; color:#bdcde7}
  .map-key span::before {content:'➝'; font-weight:bold; margin-right:.35rem}
  .map-key .facing::before {color:#75b8ff}
  .map-key .travel::before {color:#82f0be}
  .map-key .turn::before {content:'↻'; color:#ffcb73}
  .map-key .path::before {content:'┄'; color:#b9a4ff}
  .map-key .obstacle::before {content:'■'; color:#ff9c87}
  #goal-form input[type="number"] {width:5rem}
  .coordinates {display:flex; flex-wrap:wrap; gap:.6rem; margin-top:.65rem}
  .pose-readout {display:grid; grid-template-columns:max-content 1fr; gap:.3rem .7rem; margin:.8rem 0; font-size:.9rem}
  .pose-readout dt {color:#bdcde7}
  .pose-readout dd {margin:0}
  .command-readout {padding:.8rem; border:1px solid #455f7f; border-radius:.5rem; background:#17243a}
  .command-readout p {margin:.35rem 0 0; overflow-wrap:anywhere}
  .position-setup {margin:.7rem 0; padding:.5rem .75rem; border-left:3px solid #ffcb73; background:#292725}
  .position-setup p {margin:.25rem 0}
  .marker {position:absolute; width:.8rem; height:.8rem; border:2px solid #17243a; border-radius:50%; background:#84e9cf; transform:translate(-50%,50%)}
  .marker.start {background:#ffcd77}
  .marker.goal {background:#b9a4ff; box-shadow:0 0 0 2px #ede5ff}
  .marker-label {position:absolute; left:.9rem; bottom:-.2rem; padding:.1rem .25rem; background:#101827; color:#fff; font-size:.8rem; white-space:nowrap}
  .marker-label.left {left:auto; right:.9rem}
  #points {padding-left:1.2rem; overflow-wrap:anywhere}
</style>
<h1>JetBot 1m × 1m 예비 주행</h1>
<main>
<section id="control-panel" aria-labelledby="control-heading">
<h2 id="control-heading">주행 조작</h2>
<p id="mode">연결 확인 중</p>
<div class="command-readout">
  <strong>최근 명령</strong>
  <p id="last-command">아직 보낸 명령이 없습니다.</p>
  <p id="command-input" class="help" hidden></p>
  <p id="status" role="status">장비 응답 대기 중</p>
</div>
<div id="position-setup" class="position-setup" hidden>
  <p>지도에서 이동을 보려면 로봇의 현재 위치를 맞춰 주세요.</p>
  <button id="anchor-start" type="button" hidden>로봇을 시작점에 뒀어요</button>
  <p class="help">이미 이동했다면 실제 좌표로 현재 위치를 설정하세요.</p>
</div>
<button data-action="forward" disabled>전진</button>
<button data-action="backward" disabled>후진</button>
<button data-action="left" disabled>왼쪽 회전 · 90도 목표</button>
<button data-action="right" disabled>오른쪽 회전 · 90도 목표</button>
<button id="stop">즉시 정지</button>
<form id="command-form">
  <label for="command">문장으로 조작하기</label>
  <input id="command" type="text" maxlength="80" required placeholder="앞으로 10cm 가줘" aria-describedby="command-example">
  <button type="submit">명령 보내기</button>
  <p class="help" id="command-example">열 센치만 뒤로 빼줘 · A 지점까지 가줄래? · 방향을 35도로 맞춰줘 · 지금 멈춰</p>
  <p class="help" id="distance-range"></p>
</form>
<p id="error" role="alert"></p>
</section>
<section id="map-panel" aria-labelledby="map-heading">
<h2 id="map-heading">1m 시험 공간과 로봇 위치</h2>
<p class="help">왼쪽 아래가 (0, 0)cm입니다. 차체 크기는 20 × 20cm이며, 위치는 모터 구동 시간으로 추정합니다.</p>
<p id="pose">현재 위치를 설정하면 지도에 로봇이 표시됩니다.</p>
<div>
  <button id="reset-position" type="button" disabled>로봇 위치 초기화</button>
  <button id="reset-start" type="button" disabled>시작점 초기화</button>
</div>
<dl class="pose-readout">
  <dt>바라보는 방향</dt><dd id="facing-readout">설정 대기</dd>
  <dt>이동 상태</dt><dd id="motion-readout">정지</dd>
</dl>
<div class="map-key"><span class="facing">바라보는 방향</span><span class="travel">이동 방향</span><span class="turn">회전 방향</span><span class="path">계획 경로</span><span class="obstacle">가상 장애물</span></div>
<div id="workspace" aria-label="1m 지도"><div id="map-points"></div><canvas id="robot-map" width="500" height="500" aria-hidden="true"></canvas></div>
<button id="pick-position" type="button" disabled>지도에서 로봇 위치 지정</button>
<button id="pick-heading" type="button" disabled>지도에서 로봇 방향 지정</button>
<button id="pick-goal" type="button" disabled>지도에서 목표 위치 지정</button>
<button id="pick-obstacle" type="button" disabled>장애물 추가</button>
<p id="map-pick-hint" class="help" role="status"></p>
<form id="goal-form" hidden>
  <label for="goal-name">목표 이름</label>
  <input id="goal-name" type="text" maxlength="24" required>
  <div class="coordinates">
    <label>X (cm) <input id="goal-x" type="number" min="0" max="100" required></label>
    <label>Y (cm) <input id="goal-y" type="number" min="0" max="100" required></label>
  </div>
  <button type="button" id="pick-edit" hidden>새 목표 위치를 지도에서 선택</button>
  <button type="submit" id="save-goal">목표 등록 및 경로 찾기</button>
  <button type="button" id="cancel-goal">취소</button>
</form>
<div id="route-controls" hidden>
  <p><strong id="route-name"></strong></p>
  <p id="route-status"></p>
  <button id="route-run" type="button" disabled>전체 경로 실행</button>
  <button id="route-step" type="button" disabled>다음 동작 실행</button>
  <button id="route-clear" type="button" disabled>경로 해제</button>
  <p class="help" id="route-run-hint"></p>
  <p class="help">실제 장애물 위치를 지도와 맞추고, 전체 경로 실행 중에는 화면을 열어 두세요.</p>
</div>
<ul id="points"></ul>
<ul id="obstacles" aria-label="가상 장애물 목록"></ul>
</section>
</main>
<script>
const token = '__TOKEN__';
const motionButtons = document.querySelectorAll('[data-action]');
const error = document.getElementById('error');
const forwardButton = document.querySelector('[data-action="forward"]');
const backwardButton = document.querySelector('[data-action="backward"]');
const points = document.getElementById('points');
const obstacles = document.getElementById('obstacles');
const pointLayer = document.getElementById('map-points');
const robotMap = document.getElementById('robot-map');
const bodyHalf = __ROBOT_SIZE_CM__ * robotMap.width / (2 * 100);
const mapContext = robotMap.getContext('2d');
const workspace = document.getElementById('workspace');
const pickHint = document.getElementById('map-pick-hint');
const positionPicker = document.getElementById('pick-position');
const headingPicker = document.getElementById('pick-heading');
const goalPicker = document.getElementById('pick-goal');
const obstaclePicker = document.getElementById('pick-obstacle');
const goalForm = document.getElementById('goal-form');
const goalNameInput = document.getElementById('goal-name');
const goalXInput = document.getElementById('goal-x');
const goalYInput = document.getElementById('goal-y');
const editPicker = document.getElementById('pick-edit');
const saveGoal = document.getElementById('save-goal');
const routeControls = document.getElementById('route-controls');
const routeRun = document.getElementById('route-run');
const routeStep = document.getElementById('route-step');
const routeClear = document.getElementById('route-clear');
let selection = null;
let editingName = null;
let obstacleCorner = null;
let renderedObstacles = '';
let robotPose = null;
const poseLabel = document.getElementById('pose');
const facingLabel = document.getElementById('facing-readout');
const motionLabel = document.getElementById('motion-readout');
const anchorButton = document.getElementById('anchor-start');
const resetPositionButton = document.getElementById('reset-position');
const resetStartButton = document.getElementById('reset-start');
const lastCommandLabel = document.getElementById('last-command');
const commandInputLabel = document.getElementById('command-input');
const directions = ['오른쪽','오른쪽 위','위쪽','왼쪽 위','왼쪽','왼쪽 아래','아래쪽','오른쪽 아래'];
let renderedPoints = '';
let pending = false;
let stopPending = false;
let generation = 0;
let activeRun = null;
function render(state) {
  if (state.generation < generation) return;
  generation = state.generation;
  if (activeRun !== state.route_run) activeRun = null;
  document.getElementById('mode').textContent = state.mode === 'live' ? '실제 JetBot 제어' : '모의 시험';
  document.getElementById('status').textContent = state.message;
  const lastCommand = state.last_command;
  lastCommandLabel.textContent = lastCommand ? lastCommand.action : '아직 보낸 명령이 없습니다.';
  commandInputLabel.textContent = lastCommand?.text || '';
  commandInputLabel.hidden = !lastCommand?.text;
  document.getElementById('position-setup').hidden = Boolean(state.estimated_pose);
  anchorButton.hidden = !state.points['시작점'];
  const busy = pending || stopPending || state.status !== 'ready';
  anchorButton.disabled = busy;
  resetPositionButton.disabled = busy || !state.estimated_pose;
  resetStartButton.disabled = busy || !state.points['시작점'];
  robotPose = state.estimated_pose;
  setMapControls(busy);
  renderRouteControls(state.route_plan, busy);
  forwardButton.textContent = state.forward_cm + 'cm 직진';
  backwardButton.textContent = state.forward_cm + 'cm 후진';
  renderPose(state.estimated_pose, state.motion, state.status, state.points['시작점'], state.route_plan, state.obstacles);
  renderObstacleList(state.obstacles, busy);
  document.getElementById('distance-range').textContent =
    '한 번에 ' + state.forward_range_cm.join('~') + 'cm 설정 가능 · 주행 전 바닥에서 남은 거리를 확인하세요.';
  for (const button of motionButtons) button.disabled = pending || stopPending || state.status !== 'ready';
  const goalName = state.route_plan?.name;
  const nextPoints = JSON.stringify([state.points, goalName]);
  if (nextPoints === renderedPoints) return;
  renderedPoints = nextPoints;
  points.textContent = '';
  pointLayer.textContent = '';
  for (const [name, [x, y]] of Object.entries(state.points)) {
    const item = document.createElement('li');
    const coordinateLabel = name + ' (' + x + ', ' + y + ')cm';
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = coordinateLabel;
    button.disabled = busy;
    button.onclick = () => send('route-target', {name});
    item.appendChild(button);
    if (name !== '시작점') {
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.textContent = '수정';
      edit.setAttribute('aria-label', name + ' 수정');
      edit.disabled = busy;
      edit.onclick = () => openGoalForm(name, x, y);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '삭제';
      remove.setAttribute('aria-label', name + ' 삭제');
      remove.disabled = busy;
      remove.onclick = async () => {
        if (await send('goal-delete', {name}) && editingName === name) closeGoalForm();
      };
      item.append(edit, remove);
    }
    points.appendChild(item);
    const marker = document.createElement('span');
    marker.className = name === goalName ? 'marker goal' : name === '시작점' ? 'marker start' : 'marker';
    marker.style.left = x + '%';
    marker.style.bottom = y + '%';
    marker.title = coordinateLabel;
    const label = document.createElement('span');
    label.className = x > 65 ? 'marker-label left' : 'marker-label';
    label.textContent = name;
    marker.appendChild(label);
    pointLayer.appendChild(marker);
  }
}
function setMapControls(busy) {
  positionPicker.disabled = goalPicker.disabled = obstaclePicker.disabled = saveGoal.disabled = editPicker.disabled = busy;
  headingPicker.disabled = busy || !robotPose;
  for (const button of document.querySelectorAll('#points button,#obstacles button')) button.disabled = busy;
}
function renderObstacleList(boxes, busy) {
  const serialized = JSON.stringify(boxes);
  if (serialized === renderedObstacles) return;
  renderedObstacles = serialized;
  obstacles.textContent = '';
  for (const [id, [x1, y1, x2, y2]] of Object.entries(boxes)) {
    const item = document.createElement('li');
    item.textContent = '장애물 ' + id + ' · ' + (x2 - x1) + ' × ' + (y2 - y1) + 'cm ';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '삭제';
    remove.setAttribute('aria-label', '장애물 ' + id + ' 삭제');
    remove.disabled = busy;
    remove.onclick = () => send('obstacle-delete', {id});
    item.appendChild(remove);
    obstacles.appendChild(item);
  }
}
function directionName(heading) {
  return directions[Math.round(((heading % 360 + 360) % 360) / 45) % directions.length];
}
function arrowHead(x, y, angle, color) {
  mapContext.fillStyle = color;
  mapContext.beginPath();
  mapContext.moveTo(x, y);
  for (const wing of [-0.5, 0.5]) {
    mapContext.lineTo(x - 11 * Math.cos(angle + wing), y - 11 * Math.sin(angle + wing));
  }
  mapContext.closePath();
  mapContext.fill();
}
function arrow(x, y, angle, from, to, color) {
  const dx = Math.cos(angle), dy = Math.sin(angle);
  mapContext.strokeStyle = color;
  mapContext.lineWidth = 4;
  mapContext.beginPath();
  mapContext.moveTo(x + dx * from, y + dy * from);
  mapContext.lineTo(x + dx * (to - 4), y + dy * (to - 4));
  mapContext.stroke();
  arrowHead(x + dx * to, y + dy * to, angle, color);
}
function drawRobotIcon(x, y, heading, preview) {
  mapContext.save();
  mapContext.translate(x, y);
  mapContext.rotate(heading === null ? 0 : (90 - heading) * Math.PI / 180);
  mapContext.fillStyle = preview ? '#8499b32b' : '#397ed24d';
  mapContext.fillRect(-bodyHalf, -bodyHalf, 2 * bodyHalf, 2 * bodyHalf);
  mapContext.strokeStyle = preview ? '#b4c2d4' : '#9ecaff';
  mapContext.lineWidth = 2.5;
  if (preview) mapContext.setLineDash([4, 3]);
  mapContext.strokeRect(-bodyHalf, -bodyHalf, 2 * bodyHalf, 2 * bodyHalf);
  if (!preview) {
    mapContext.strokeStyle = '#e0efff';
    mapContext.lineWidth = 4;
    mapContext.beginPath();
    mapContext.moveTo(-bodyHalf * .55, -bodyHalf + 6);
    mapContext.lineTo(bodyHalf * .55, -bodyHalf + 6);
    mapContext.stroke();
  }
  mapContext.fillStyle = preview ? '#9db6d0' : '#80b5ff';
  mapContext.fillRect(-12, -8, 24, 17);
  mapContext.fillRect(-1.5, -13, 3, 5);
  mapContext.fillStyle = '#101c30';
  mapContext.fillRect(-7, -3, 4.5, 5);
  mapContext.fillRect(2.5, -3, 4.5, 5);
  mapContext.restore();

  const label = preview ? 'JetBot · 위치 확인 전' : 'JetBot';
  mapContext.font = '600 16px system-ui, sans-serif';
  const labelWidth = mapContext.measureText(label).width + 16;
  const labelX = Math.max(4, Math.min(x - labelWidth / 2, robotMap.width - labelWidth - 4));
  const labelY = Math.min(robotMap.height - 25, Math.max(2,
    y >= bodyHalf + 34 ? y - bodyHalf - 32 : y + bodyHalf + 8));
  mapContext.fillStyle = '#101c30';
  mapContext.fillRect(labelX, labelY, labelWidth, 23);
  mapContext.fillStyle = '#f1f6ff';
  mapContext.fillText(label, labelX + 8, labelY + 16);
}
function drawRobot(x, y, heading, action) {
  const angle = -heading * Math.PI / 180;
  drawRobotIcon(x, y, heading, false);
  arrow(x, y, angle, 21, bodyHalf - 10, '#75b8ff');
  if (action === 'forward' || action === 'backward') {
    const reverse = action === 'backward';
    arrow(x, y, reverse ? angle + Math.PI : angle, reverse ? 18 : bodyHalf - 5, bodyHalf + 14, '#82f0be');
  } else if (action === 'left' || action === 'right') {
    const left = action === 'left';
    const end = angle + (left ? -1.65 : 1.65);
    mapContext.strokeStyle = '#ffcb73';
    mapContext.lineWidth = 4;
    mapContext.beginPath();
    const radius = bodyHalf * .72;
    mapContext.arc(x, y, radius, angle, end, left);
    mapContext.stroke();
    arrowHead(x + radius * Math.cos(end), y + radius * Math.sin(end),
      end + (left ? -Math.PI / 2 : Math.PI / 2), '#ffcb73');
  }
}
function renderRouteControls(plan, busy) {
  routeControls.hidden = !plan;
  routeRun.disabled = busy || !plan?.auto_ready;
  routeStep.disabled = busy || !plan?.next;
  routeClear.disabled = busy;
  if (!plan) return;
  routeRun.textContent = plan.near_goal ? '근처 도착점까지 실행' : '전체 경로 실행';
  document.getElementById('route-name').textContent = plan.name;
  document.getElementById('route-status').textContent = plan.message;
  document.getElementById('route-run-hint').textContent = plan.auto_hint || '';
}
function drawRoute(plan) {
  if (!plan?.path?.length && !plan?.near_goal) return;
  mapContext.save();
  mapContext.strokeStyle = '#b9a4ff';
  mapContext.lineWidth = 3;
  mapContext.setLineDash([7, 6]);
  mapContext.beginPath();
  const scale = robotMap.width / 100;
  plan.path.forEach(([x, y], index) => {
    if (index === 0) mapContext.moveTo(x * scale, (100 - y) * scale);
    else mapContext.lineTo(x * scale, (100 - y) * scale);
  });
  mapContext.stroke();
  if (plan.near_goal) {
    const [x, y] = plan.near_goal;
    mapContext.setLineDash([]);
    mapContext.fillStyle = '#ffcb73';
    mapContext.beginPath();
    mapContext.arc(x * scale, (100 - y) * scale, 5, 0, 2 * Math.PI);
    mapContext.fill();
  }
  mapContext.restore();
}
function renderPose(pose, action, status, startPoint, plan, boxes) {
  mapContext.clearRect(0, 0, robotMap.width, robotMap.height);
  const scale = robotMap.width / 100;
  mapContext.save();
  mapContext.fillStyle = '#fb806e65';
  mapContext.strokeStyle = '#ff9c87';
  mapContext.lineWidth = 2;
  for (const [x1, y1, x2, y2] of Object.values(boxes)) {
    const rect = [x1 * scale, (100 - y2) * scale, (x2 - x1) * scale, (y2 - y1) * scale];
    mapContext.fillRect(...rect);
    mapContext.strokeRect(...rect);
  }
  if (obstacleCorner) {
    mapContext.fillStyle = '#ff9c87';
    mapContext.fillRect(obstacleCorner.x * scale - 4, (100 - obstacleCorner.y) * scale - 4, 8, 8);
  }
  mapContext.restore();
  drawRoute(plan);
  const turning = action === 'left' ? '왼쪽 제자리 회전' : '오른쪽 제자리 회전';
  const traveling = action === 'forward' || action === 'backward';
  const travel = action === 'forward' ? '전진' : '후진';
  motionLabel.textContent = traveling ? travel : action ? turning : status === 'fault' ? '정지 상태 확인 필요' : '정지';
  if (!pose) {
    poseLabel.textContent = startPoint
      ? '로봇 그림을 시작점에 표시했습니다. 실측 좌표와 방향을 입력해 위치를 맞춰 주세요.'
      : '로봇 그림을 지도 중앙에 표시했습니다. 실측 좌표와 방향을 입력해 위치를 맞춰 주세요.';
    facingLabel.textContent = '설정 대기';
    const [x, y] = startPoint || [50, 50];
    drawRobotIcon(x * robotMap.width / 100, (100 - y) * robotMap.height / 100, null, true);
    return;
  }
  const [x, y, heading] = pose;
  const inside = x >= 0 && x <= 100 && y >= 0 && y <= 100;
  poseLabel.textContent = '추정 위치 (' + x.toFixed(1) + ', ' + y.toFixed(1) + ')cm' +
    (inside ? '' : ' · 지도 범위 밖으로 추정');
  facingLabel.textContent = directionName(heading) + ' · ' + (Math.round(heading) % 360) + '°';
  if (traveling) {
    motionLabel.textContent = travel + ' · ' + directionName(heading + (action === 'backward' ? 180 : 0));
  }
  if (inside) drawRobot(x * robotMap.width / 100, (100 - y) * robotMap.height / 100, heading, action);
}
async function poll() {
  let delay = 350;
  try {
    const response = await fetch('/state', {cache:'no-store'});
    if (!response.ok) throw new Error();
    const state = await response.json();
    render(state);
    if (state.status === 'moving') delay = 120;
  } catch {
    document.getElementById('status').textContent = '장비 응답을 확인할 수 없습니다.';
    poseLabel.textContent = '연결이 복구되면 추정 위치를 갱신합니다.';
    facingLabel.textContent = motionLabel.textContent = '응답 대기';
    anchorButton.disabled = resetPositionButton.disabled = resetStartButton.disabled = true;
    setMapControls(true);
    routeRun.disabled = routeStep.disabled = routeClear.disabled = true;
    mapContext.clearRect(0, 0, robotMap.width, robotMap.height);
    for (const button of motionButtons) button.disabled = true;
  }
  setTimeout(poll, delay);
}
async function send(action, payload) {
  const stop = action === 'stop';
  if (stopPending || (!stop && pending)) return;
  if (stop) stopPending = true;
  else pending = true;
  for (const button of motionButtons) button.disabled = true;
  anchorButton.disabled = resetPositionButton.disabled = resetStartButton.disabled = true;
  setMapControls(true);
  routeRun.disabled = routeStep.disabled = routeClear.disabled = true;
  error.textContent = '';
  try {
    const headers = {'X-Trial-Token':token, 'X-Trial-Generation':String(generation)};
    const options = {method:'POST', headers};
    if (payload !== undefined) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(payload);
    }
    const response = await fetch('/' + action, options);
    const state = await response.json();
    if (!response.ok) error.textContent = state.message;
    else {
      if (action === 'route-run' || action === 'command') activeRun = state.route_run;
      render(state);
      return true;
    }
  } catch {
    error.textContent = '명령의 처리 결과를 확인할 수 없습니다.';
  } finally {
    if (stop) stopPending = false;
    else pending = false;
  }
}
for (const button of motionButtons) button.onclick = () => send(button.dataset.action);
document.getElementById('stop').onclick = () => send('stop');
anchorButton.onclick = () => send('anchor');
resetPositionButton.onclick = () => send('reset-position');
resetStartButton.onclick = () => send('reset-start');
function closeGoalForm() {
  editingName = null;
  goalForm.hidden = true;
  editPicker.hidden = true;
  selection = null;
  delete workspace.dataset.pick;
  pickHint.textContent = '';
}
function openGoalForm(name, x, y) {
  editingName = name;
  goalNameInput.value = name || '';
  goalXInput.value = x;
  goalYInput.value = y;
  editPicker.hidden = name === null;
  saveGoal.textContent = name === null ? '목표 등록 및 경로 찾기' : '수정 내용 저장';
  goalForm.hidden = false;
  selection = null;
  obstacleCorner = null;
  delete workspace.dataset.pick;
  pickHint.textContent = name ? '수정할 이름과 좌표를 입력하세요.' : '선택한 목표 (' + x + ', ' + y + ')cm';
  goalNameInput.focus();
}
function pick(kind, hint) {
  if (kind !== 'edit') closeGoalForm();
  selection = kind;
  workspace.dataset.pick = kind;
  obstacleCorner = null;
  pickHint.textContent = hint;
}
positionPicker.onclick = () => pick('position', '지도에서 로봇 중심을 클릭하세요.');
headingPicker.onclick = () => pick('heading', '지도에서 로봇 앞면이 향하는 쪽을 클릭하세요.');
goalPicker.onclick = () => pick('goal', '지도에서 목표점을 클릭하세요.');
obstaclePicker.onclick = () => pick('obstacle', '가상 장애물의 첫 번째 모서리를 클릭하세요.');
editPicker.onclick = () => pick('edit', '지도에서 목표의 새 위치를 클릭하세요.');
document.getElementById('cancel-goal').onclick = closeGoalForm;
workspace.onclick = async event => {
  if (!selection || positionPicker.disabled) return;
  const bounds = robotMap.getBoundingClientRect();
  const x = Math.round((event.clientX - bounds.left) * 100 / bounds.width);
  const y = Math.round((bounds.bottom - event.clientY) * 100 / bounds.height);
  if (x < 0 || x > 100 || y < 0 || y > 100) return;
  const kind = selection;
  if (kind === 'obstacle' && !obstacleCorner) {
    obstacleCorner = {x, y};
    pickHint.textContent = '장애물의 맞은편 모서리를 클릭하세요.';
    return;
  }
  if (kind === 'obstacle' && (obstacleCorner.x === x || obstacleCorner.y === y)) {
    pickHint.textContent = '장애물의 가로와 세로 길이가 생기도록 맞은편 모서리를 클릭하세요.';
    return;
  }
  if (kind === 'heading' && (!robotPose || Math.hypot(x - robotPose[0], y - robotPose[1]) < 2)) {
    pickHint.textContent = '로봇 중심에서 떨어진 방향점을 클릭하세요.';
    return;
  }
  selection = null;
  delete workspace.dataset.pick;
  if (kind === 'position') {
    if (await send('map-position', {x, y})) {
      pickHint.textContent = '로봇 위치를 지정했습니다. 실제 방향은 로봇 방향 지정에서 맞출 수 있습니다.';
    }
  } else if (kind === 'heading') {
    const degrees = (Math.round(Math.atan2(y - robotPose[1], x - robotPose[0]) * 180 / Math.PI) + 360) % 360;
    if (await send('map-direction', {degrees})) pickHint.textContent = '로봇 방향을 ' + degrees + '°로 지정했습니다.';
  } else if (kind === 'obstacle') {
    const first = obstacleCorner;
    obstacleCorner = null;
    if (await send('obstacle-add', {x1:first.x, y1:first.y, x2:x, y2:y})) {
      pickHint.textContent = '가상 장애물을 등록했습니다.';
    }
  } else if (kind === 'edit') {
    goalXInput.value = x;
    goalYInput.value = y;
    pickHint.textContent = '새 좌표를 확인하고 수정 내용을 저장하세요.';
  } else {
    openGoalForm(null, x, y);
  }
};
goalForm.onsubmit = async event => {
  event.preventDefault();
  const payload = {x:goalXInput.valueAsNumber, y:goalYInput.valueAsNumber, name:goalNameInput.value};
  if (editingName !== null) payload.oldName = editingName;
  if (await send(editingName === null ? 'map-goal' : 'goal-edit', payload)) closeGoalForm();
};
routeStep.onclick = () => send('route-step');
routeClear.onclick = () => send('route-clear');
routeRun.onclick = () => send('route-run');
async function keepRouteAlive() {
  if (activeRun !== null) {
    try {
      await fetch('/route-keepalive', {method:'POST', headers:{
        'X-Trial-Token':token, 'X-Route-Run':String(activeRun)
      }});
    } catch {
      // The server stops the run if heartbeats stop arriving.
    }
  }
  setTimeout(keepRouteAlive, 300);
}
keepRouteAlive();
document.getElementById('command-form').onsubmit = event => {
  event.preventDefault();
  send('command', {text:document.getElementById('command').value});
};
poll();
</script></html>"""


def intersects_box(start, end, box):
    enter, leave = 0.0, 1.0
    for origin, target, low, high in zip(start, end, box[:2], box[2:]):
        delta = target - origin
        if abs(delta) < 1e-9:
            if not low <= origin <= high:
                return False
        else:
            first, last = sorted(((low - origin) / delta, (high - origin) / delta))
            enter, leave = max(enter, first), min(leave, last)
            if enter > leave:
                return False
    return True


def body_inside(point, extent):
    return all(extent - 1e-6 <= axis <= SPACE_CM - extent + 1e-6 for axis in point)


def body_extent(heading):
    radians = math.radians(heading)
    return ROBOT_SIZE_CM / 2 * (abs(math.cos(radians)) + abs(math.sin(radians)))


def travel_heading(start, end):
    return math.degrees(math.atan2(end[1] - start[1], end[0] - start[0])) % 360


def steering(heading, direction):
    turn = (direction - heading + 180) % 360 - 180
    if abs(turn) > 90:
        return 'backward', turn - math.copysign(180, turn)
    return 'forward', turn


class MotionControl:
    def __init__(self, motor, speed, forward_durations, turn_seconds, mode):
        self.motor = motor
        self.speed = speed
        self.mode = mode
        self.forward_durations = forward_durations
        self.forward_cm = TARGET_FORWARD_CM
        self.points = {}
        self.obstacles = {}
        self.next_obstacle_id = 1
        self.cached_route = (None, None)
        self.goal_name = None
        self.pose = None
        self.pose_motion = None
        self.turn_degrees = MEASURED_TURN_DEGREES * turn_seconds / PREVIOUS_TURN_SECONDS
        self.actions = {
            'left': ('왼쪽 회전', turn_seconds),
            'right': ('오른쪽 회전', turn_seconds),
        }
        self.lock = threading.RLock()
        self.timer = None
        self.generation = 0
        self.status = 'ready'
        self.message = '동작 준비 완료'
        self.last_command = None
        self.route_run = None
        self.route_queue = []
        self.route_path = []
        self.route_near_goal = None
        self.route_total = 0
        self.route_deadline = 0
        self.route_watchdog = None

    def snapshot(self):
        with self.lock:
            return {'mode': self.mode, 'status': self.status, 'message': self.message,
                    'generation': self.generation, 'forward_cm': self.forward_cm,
                    'forward_range_cm': [min(self.forward_durations), max(self.forward_durations)],
                    'points': self.points.copy(), 'obstacles': self.obstacles.copy(),
                    'estimated_pose': self.estimated_pose(),
                    'motion': self.pose_motion[-1] if self.pose_motion else None,
                    'last_command': self.last_command, 'route_plan': self.route_plan(),
                    'route_run': self.route_run}

    def route_plan(self):
        if self.goal_name is None:
            return None
        if self.route_run is not None:
            return {'name': self.goal_name, 'path': self.route_path, 'next': None,
                    'near_goal': self.route_near_goal,
                    'message': '전체 경로 실행 중 ({}/{})'.format(
                        self.route_total - len(self.route_queue), self.route_total)}
        goal = self.points[self.goal_name]
        key = (self.goal_name, goal, self.pose, tuple(self.obstacles.values()))
        if self.cached_route[0] != key:
            self.cached_route = (key, self.calculate_route(goal))
        return self.cached_route[1]

    def obstacle_buffers(self, extent=BODY_RADIUS_CM):
        c = extent + OBSTACLE_MARGIN_CM
        return [(x1 - c, y1 - c, x2 + c, y2 + c) for x1, y1, x2, y2 in self.obstacles.values()]

    def calculate_route(self, goal):
        plan = {'name': self.goal_name, 'path': [], 'next': None}
        if self.pose is None:
            plan['message'] = '지도에서 로봇의 현재 위치와 방향을 지정해 주세요.'
            return plan
        start, heading = self.pose[:2], self.pose[2]
        boxes = self.obstacle_buffers()
        extent = body_extent(heading)
        if not body_inside(start, extent) or any(
                intersects_box(start, start, box) for box in self.obstacle_buffers(extent)):
            plan['message'] = '현재 차체가 장애물이나 시험 공간 경계에 너무 가깝습니다. 실제 위치와 방향을 확인해 주세요.'
            return plan
        if math.hypot(goal[0] - start[0], goal[1] - start[1]) < 0.5:
            plan['message'] = '추정 위치가 목표에 도착했습니다. 바닥에서 실제 위치를 확인해 주세요.'
            return plan
        path = self.find_path(start, goal, heading, boxes)
        if not path and not body_inside(goal, BODY_RADIUS_CM):
            offsets = (0, -1, 1, -3, 3, -5, 5, -7, 7)
            nearby = sorted(
                (math.hypot(dx, dy), (goal[0] + dx, goal[1] + dy))
                for dx in offsets for dy in offsets
                if 0 < math.hypot(dx, dy) <= NEAR_GOAL_LIMIT_CM
                and body_inside((goal[0] + dx, goal[1] + dy),
                                ROBOT_SIZE_CM / 2 + NEAR_GOAL_WALL_MARGIN_CM)
            )
            for _, arrival in nearby:
                if math.hypot(arrival[0] - start[0], arrival[1] - start[1]) < 0.5:
                    plan.update(near_goal=arrival, message='추정 위치가 목표 근처 도착점에 있습니다.')
                    return plan
                path = self.find_path(start, arrival, heading, boxes, NEAR_GOAL_WALL_MARGIN_CM)
                if path:
                    plan['near_goal'] = arrival
                    break
        if not path:
            plan['message'] = '차체가 지나갈 경로를 찾지 못했습니다. 시작 위치, 장애물 간격 또는 목표를 확인해 주세요.'
            return plan
        segments = []
        for first, second in zip(path, path[1:]):
            direction = travel_heading(first, second)
            action, turn = steering(heading, direction)
            if abs(turn) > 1e-7:
                pulses = math.ceil(abs(turn) / self.turn_degrees)
                segments.extend([('left' if turn > 0 else 'right', abs(turn) / pulses)] * pulses)
                heading += turn
            distance = math.hypot(second[0] - first[0], second[1] - first[1])
            segments.extend([(action, self.route_travel_distance(distance))] *
                            math.ceil(distance / max(self.forward_durations)))
        action, amount = segments[0]
        labels = {'left': '° 왼쪽 회전', 'right': '° 오른쪽 회전',
                  'forward': 'cm 전진', 'backward': 'cm 후진'}
        hint = '전체 경로 · {}개 동작'.format(len(segments))
        if 'near_goal' in plan:
            hint = '목표가 경계에 가까워 노란색 도착점 ({:g}, {:g})cm까지 이동합니다. · {}개 동작'.format(
                *plan['near_goal'], len(segments))
        plan.update(path=path, next=segments[0], auto_ready=True, segments=segments,
                    message='다음 동작 {:.1f}{}'.format(amount, labels[action]),
                    auto_hint=hint)
        return plan

    def route_travel_distance(self, distance):
        return distance / math.ceil(distance / max(self.forward_durations))

    def find_path(self, start, goal, heading, boxes, arrival_margin=0):
        # Each intermediate point has room for the square chassis to turn in place.
        inset = BODY_RADIUS_CM + 0.5
        nodes = [start, goal]
        candidates = [(start[0], goal[1]), (goal[0], start[1])]
        for x1, y1, x2, y2 in boxes:
            for x in (x1 - 0.5, x2 + 0.5):
                for y in (y1 - 0.5, y2 + 0.5):
                    candidates.append((x, y))
        for point in candidates:
            corner = tuple(max(inset, min(SPACE_CM - inset, axis)) for axis in point)
            if corner not in nodes and not any(intersects_box(corner, corner, box) for box in boxes):
                nodes.append(corner)
        costs = {0: 0.0}
        parents = {}
        queue = [(0.0, 0)]
        minimum = min(self.forward_durations)
        while queue:
            cost, index = heapq.heappop(queue)
            if cost > costs[index]:
                continue
            if index == 1:
                path = [goal]
                while index:
                    index = parents[index]
                    path.append(nodes[index])
                return path[::-1]
            first = nodes[index]
            for neighbor, second in enumerate(nodes[1:], 1):
                distance = math.hypot(second[0] - first[0], second[1] - first[1])
                new_cost = cost + distance
                if distance < minimum or new_cost >= costs.get(neighbor, math.inf):
                    continue
                direction = travel_heading(first, second)
                extent = body_extent(direction)
                margin = arrival_margin if neighbor == 1 else 0
                if not body_inside(first, extent) or not body_inside(second, extent + margin):
                    continue
                if index == 0 and abs(steering(heading, direction)[1]) > 1e-7:
                    if not body_inside(first, BODY_RADIUS_CM) or any(
                            intersects_box(first, first, box) for box in boxes):
                        continue
                if any(intersects_box(first, second, box) for box in self.obstacle_buffers(extent)):
                    continue
                travel = self.route_travel_distance(distance)
                if math.floor(travel) not in self.forward_durations or math.ceil(travel) not in self.forward_durations:
                    continue
                costs[neighbor] = new_cost
                parents[neighbor] = index
                heapq.heappush(queue, (new_cost, neighbor))
        return None

    def run_route(self, generation, target=None, command_text=None):
        with self.lock:
            self.require_generation(generation)
            if self.status != 'ready':
                raise ValueError('현재 동작이 끝난 뒤 실행해 주세요.')
            if target is not None:
                if target not in self.points:
                    raise ValueError('지도에 등록된 목표를 선택해 주세요.')
                self.goal_name = target
                self.generation += 1
            plan = self.route_plan()
            if not plan or not plan.get('auto_ready'):
                raise ValueError((plan.get('auto_hint') or plan['message']) if plan else
                                 '지도에서 목표를 먼저 지정해 주세요.')
            if target is not None and plan.get('near_goal'):
                raise ValueError('지도에 표시된 노란색 도착점을 확인하고 근처 도착점까지 실행을 눌러 주세요.')
            self.route_queue = list(plan['segments'])
            self.route_path = plan['path']
            self.route_near_goal = plan.get('near_goal')
            self.route_total = len(self.route_queue)
            self.generation += 1
            self.route_run = self.generation
            self.route_deadline = time.monotonic() + ROUTE_LINK_TIMEOUT_S
            self.watch_route(self.route_run)
            return self.start_route_segment(command_text)

    def start_route_segment(self, command_text=None):
        action, value = self.route_queue.pop(0)
        try:
            return self.start(action, str(self.generation), value, command_text)
        except ValueError as error:
            return self.stop(str(error))

    def route_keepalive(self, run):
        with self.lock:
            now = time.monotonic()
            if self.route_run is not None and run == str(self.route_run) and now < self.route_deadline:
                self.route_deadline = now + ROUTE_LINK_TIMEOUT_S

    def watch_route(self, run):
        with self.lock:
            if run != self.route_run:
                return
            remaining = self.route_deadline - time.monotonic()
            if remaining <= 0:
                self.stop('관제 연결이 끊겨 전체 경로를 정지했습니다.')
                return
            self.route_watchdog = threading.Timer(remaining, self.watch_route, args=(run,))
            self.route_watchdog.daemon = True
            self.route_watchdog.start()

    def route_step(self, generation):
        with self.lock:
            self.require_generation(generation)
            if self.status != 'ready':
                raise ValueError('현재 동작이 끝난 뒤 다음 동작을 실행해 주세요.')
            plan = self.route_plan()
            if not plan or not plan['next']:
                raise ValueError(plan['message'] if plan else '지도에서 목표를 먼저 지정해 주세요.')
            action, value = plan['next']
            return self.start(action, generation, value)

    def estimated_pose(self):
        if self.pose is None or self.pose_motion is None:
            return self.pose
        x, y, heading = self.pose
        started, duration, distance, turn, _action = self.pose_motion
        progress = min(1.0, max(0.0, (time.monotonic() - started) / duration))
        radians = math.radians(heading)
        return (x + distance * progress * math.cos(radians),
                y + distance * progress * math.sin(radians),
                (heading + turn * progress) % 360)

    def require_generation(self, generation):
        if generation != str(self.generation):
            raise ValueError('화면에 표시된 상태를 확인하고 다시 입력해 주세요.')

    def forward_duration(self, distance):
        low, high = math.floor(distance), math.ceil(distance)
        if low not in self.forward_durations or high not in self.forward_durations:
            minimum, maximum = min(self.forward_durations), max(self.forward_durations)
            raise ValueError('직진 거리는 장비 보정 범위인 {}~{}cm로 입력해 주세요.'.format(minimum, maximum))
        low_duration, high_duration = self.forward_durations[low], self.forward_durations[high]
        return low_duration + (distance - low) * (high_duration - low_duration)

    @staticmethod
    def validate_coordinates(x, y):
        if any(type(axis) not in (int, float) or not 0 <= axis <= SPACE_CM for axis in (x, y)):
            raise ValueError('지도 좌표는 x와 y 모두 0~100cm로 입력해 주세요.')

    @staticmethod
    def validate_goal_name(name):
        if not re.fullmatch(r'[가-힣A-Za-z0-9 ]{1,24}', name) or name in ('시작점', '현재 위치', '현재위치'):
            raise ValueError('목표 이름은 한글·영문·숫자로 입력해 주세요. 시작점과 현재 위치는 별도로 관리됩니다.')

    def configure(self, action, value, generation, command_text=None):
        with self.lock:
            self.require_generation(generation)
            if self.status != 'ready':
                raise ValueError('동작이 끝나고 준비 상태가 된 뒤 설정해 주세요.')
            if action == 'distance':
                self.forward_duration(value)
                self.forward_cm = value
                label = '전진·후진 거리 {}cm 설정'.format(value)
                self.message = '전진·후진 거리 {}cm로 설정했습니다.'.format(value)
            elif action == 'heading':
                if self.pose is None:
                    raise ValueError('지도에서 현재 위치를 먼저 설정해 주세요.')
                if not 0 <= value <= 360:
                    raise ValueError('현재 방향을 0~360도로 입력해 주세요.')
                value %= 360
                self.pose = (*self.pose[:2], value)
                label = '현재 방향 {}° 설정'.format(value)
                self.message = '현재 방향을 {}°로 설정했습니다.'.format(value)
            elif action == 'reset-position':
                self.pose = None
                label = '로봇 위치 초기화'
                self.message = '추정 위치와 방향을 초기화했습니다.'
            elif action == 'reset-start':
                self.points.pop('시작점', None)
                if self.goal_name == '시작점':
                    self.goal_name = None
                label = '시작점 초기화'
                self.message = '지도에서 시작점 표시를 지웠습니다.'
            elif action == 'route-clear':
                self.goal_name = None
                label = '경로 해제'
                self.message = '목표 지점까지의 경로를 화면에서 지웠습니다.'
            elif action == 'route-target':
                if value not in self.points:
                    raise ValueError('지도에 등록된 장소를 선택해 주세요.')
                self.goal_name = value
                label = '목표 {} 선택'.format(value)
                self.message = '{}까지의 경로를 계산했습니다.'.format(value)
            elif action == 'goal-delete':
                if value == '시작점' or value not in self.points:
                    raise ValueError('삭제할 목표를 다시 선택해 주세요.')
                del self.points[value]
                if self.goal_name == value:
                    self.goal_name = None
                label = '{} 삭제'.format(value)
                self.message = '목표 ‘{}’을(를) 삭제했습니다.'.format(value)
            elif action == 'goal-edit':
                old_name, name, x, y = value
                self.validate_coordinates(x, y)
                self.validate_goal_name(name)
                if old_name == '시작점' or old_name not in self.points:
                    raise ValueError('수정할 목표를 다시 선택해 주세요.')
                if name != old_name and name in self.points:
                    raise ValueError('같은 이름의 목표가 이미 있습니다.')
                del self.points[old_name]
                self.points[name] = (x, y)
                if self.goal_name == old_name:
                    self.goal_name = name
                label = '{} 수정'.format(name)
                self.message = '목표 ‘{}’의 이름과 좌표를 저장했습니다.'.format(name)
            elif action == 'obstacle-add':
                x1, y1, x2, y2 = value
                self.validate_coordinates(x1, y1)
                self.validate_coordinates(x2, y2)
                if x1 == x2 or y1 == y2:
                    raise ValueError('가로와 세로 길이가 생기도록 서로 마주 보는 모서리를 클릭해 주세요.')
                if len(self.obstacles) >= MAX_OBSTACLES:
                    raise ValueError('장애물은 최대 {}개까지 등록할 수 있습니다.'.format(MAX_OBSTACLES))
                obstacle_id = str(self.next_obstacle_id)
                self.next_obstacle_id += 1
                self.obstacles[obstacle_id] = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                label = '장애물 {} 등록'.format(obstacle_id)
                self.message = label + ' 완료'
            elif action == 'obstacle-delete':
                if value not in self.obstacles:
                    raise ValueError('삭제할 장애물을 다시 선택해 주세요.')
                del self.obstacles[value]
                label = '장애물 {} 삭제'.format(value)
                self.message = label + ' 완료'
            elif action == 'anchor':
                if '시작점' not in self.points:
                    raise ValueError('지도에 시작점을 먼저 등록해 주세요.')
                x, y = self.points['시작점']
                self.pose = (x, y, self.pose[2] if self.pose is not None else 0)
                label = '시작점 기준으로 현재 위치 설정'
                self.message = '현재 위치를 시작점 ({}, {})cm에 맞췄습니다.'.format(x, y)
            else:
                name, x, y = value
                self.validate_coordinates(x, y)
                if action == 'position':
                    self.pose = (x, y, self.pose[2] if self.pose is not None else 0)
                    label = '현재 위치 ({:.1f}, {:.1f})cm 설정'.format(x, y)
                    self.message = '현재 위치를 ({:.1f}, {:.1f})cm로 설정했습니다.'.format(x, y)
                else:
                    if action == 'goal':
                        self.validate_goal_name(name)
                        if name in self.points:
                            raise ValueError('같은 이름의 목표가 이미 있습니다. 목표 목록에서 수정해 주세요.')
                        self.goal_name = name
                    self.points[name] = (x, y)
                    kind = '목표' if action == 'goal' else '기준점'
                    label = '{} {} 등록'.format(name, kind)
                    self.message = '{} ‘{}’을(를) ({}, {})cm에 등록했습니다.'.format(kind, name, x, y)
            self.generation += 1
            self.last_command = {'action': label, 'text': command_text}
            return self.snapshot()

    def start(self, action, generation, value=None, command_text=None):
        with self.lock:
            self.require_generation(generation)
            if self.status != 'ready':
                raise ValueError('정지를 확인하거나 현재 동작이 끝난 뒤 다시 시도해 주세요.')
            if action in ('forward', 'backward'):
                distance = self.forward_cm if value is None else value
                duration = self.forward_duration(distance)
                label = '{:g}cm {}'.format(round(distance, 1), '전진' if action == 'forward' else '후진')
                displacement = distance if action == 'forward' else -distance
                turn = 0
            else:
                label, duration = self.actions[action]
                displacement = 0
                if value is not None:
                    duration *= value / self.turn_degrees
                    label = '{:.1f}° {}'.format(value, label)
                turn = self.turn_degrees if action == 'left' else -self.turn_degrees
                if value is not None:
                    turn = math.copysign(value, turn)
            if self.obstacles:
                if self.pose is None:
                    raise ValueError('장애물이 등록되어 있습니다. 로봇 위치와 방향을 먼저 맞춰 주세요.')
                origin = self.pose[:2]
                heading = self.pose[2]
                extent = BODY_RADIUS_CM if turn else body_extent(heading)
                radians = math.radians(heading)
                end = (origin[0] + displacement * math.cos(radians),
                       origin[1] + displacement * math.sin(radians))
                if (not body_inside(origin, extent) or not body_inside(end, extent)
                        or any(intersects_box(origin, end, box) for box in self.obstacle_buffers(extent))):
                    raise ValueError('예상 동작이 장애물이나 시험 공간 경계에 닿습니다.')
            self.last_command = {'action': label, 'text': command_text}
            timer = threading.Timer(duration, lambda: self.finish(timer, label))
            timer.daemon = True
            self.timer = timer
            try:
                getattr(self.motor, action)(self.speed)
                self.pose_motion = (time.monotonic(), duration, displacement, turn, action)
                self.status = 'moving'
                self.message = label + ' 중'
                timer.start()
            except Exception:
                self.stop()
                self.pose = None
                self.status = 'fault'
                self.message = '모터 동작 오류입니다. 정지를 확인해 주세요.'
            return self.snapshot()

    def finish(self, timer, label):
        with self.lock:
            if self.timer is not timer:
                return
            if self.route_run is None:
                self.stop(label + ' 종료')
            elif time.monotonic() >= self.route_deadline:
                self.stop('관제 연결이 끊겨 전체 경로를 정지했습니다.')
            elif self.route_queue:
                self.stop_motor(label + ' 종료')
                if self.status == 'ready':
                    self.start_route_segment()
                else:
                    self.cancel_route()
            else:
                completion = '목표 근처 도착점까지 실행했습니다.' if self.route_near_goal else '전체 경로 실행을 완료했습니다.'
                self.stop(completion + ' 실제 도착 위치를 확인해 주세요.')

    def cancel_route(self):
        self.route_run = None
        self.route_queue.clear()
        self.route_path = []
        self.route_near_goal = None
        if self.route_watchdog:
            self.route_watchdog.cancel()
            self.route_watchdog = None

    def stop(self, message='정지 명령 전송 완료', command_text=None, user_command=False):
        with self.lock:
            self.cancel_route()
            if user_command:
                self.last_command = {'action': '즉시 정지', 'text': command_text}
            self.stop_motor(message)
            return self.snapshot()

    def stop_motor(self, message):
        self.generation += 1
        if self.timer:
            self.timer.cancel()
            self.timer = None
        estimated = self.estimated_pose()
        self.pose_motion = None
        try:
            self.motor.stop()
        except Exception:
            self.pose = None
            self.status = 'fault'
            self.message = '정지 호출에 실패했습니다. 장비에서 정지 상태를 확인해 주세요.'
        else:
            self.pose = estimated
            self.status = 'ready'
            self.message = message


class LocalServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TrialHandler(BaseHTTPRequestHandler):
    def __init__(self, control, token, *args):
        self.control = control
        self.token = token
        super().__init__(*args)

    def respond(self, status, content, content_type='application/json; charset=utf-8'):
        body = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') in ('127.0.0.1:{}'.format(PORT), 'localhost:{}'.format(PORT))

    def do_GET(self):
        if not self.valid_host():
            self.send_error(403)
        elif self.path == '/':
            page = PAGE.replace('__TOKEN__', self.token).replace('__ROBOT_SIZE_CM__', str(ROBOT_SIZE_CM))
            self.respond(200, page, 'text/html; charset=utf-8')
        elif self.path == '/state':
            self.respond(200, json.dumps(self.control.snapshot()))
        else:
            self.send_error(404)

    def do_POST(self):
        token = self.headers.get('X-Trial-Token', '')
        if not self.valid_host() or not secrets.compare_digest(token.encode('utf-8'), self.token.encode('ascii')):
            self.respond(403, json.dumps({'message': '페이지를 다시 열어 주세요.'}))
            return
        action = self.path.lstrip('/')
        if action == 'route-keepalive':
            self.control.route_keepalive(self.headers.get('X-Route-Run'))
            self.respond(200, '{}')
            return
        value = None
        text = None
        map_setup = ('heading', 'goal-edit', 'goal-delete', 'obstacle-add', 'obstacle-delete')
        if action in ('command', 'map-position', 'map-goal', 'map-direction', 'route-target',
                      'goal-edit', 'goal-delete', 'obstacle-add', 'obstacle-delete'):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 512 or self.headers.get_content_type() != 'application/json':
                    raise ValueError
                data = json.loads(self.rfile.read(size).decode('utf-8'))
                if not isinstance(data, dict):
                    raise ValueError
                if action == 'command':
                    text = data.get('text')
                    if not isinstance(text, str) or len(text) > 80:
                        raise ValueError
                    with self.control.lock:
                        action, value = parse_command(text, self.control.points)
                elif action in ('route-target', 'goal-delete', 'obstacle-delete'):
                    value = data.get('id' if action == 'obstacle-delete' else 'name')
                    if not isinstance(value, str):
                        raise ValueError
                elif action == 'obstacle-add':
                    value = tuple(data.get(axis) for axis in ('x1', 'y1', 'x2', 'y2'))
                elif action == 'map-direction':
                    value = data.get('degrees')
                    if type(value) not in (int, float):
                        raise ValueError
                    action = 'heading'
                elif action == 'goal-edit':
                    old_name = data.get('oldName')
                    value = data.get('name')
                    if not isinstance(old_name, str) or not isinstance(value, str):
                        raise ValueError
                    value = (old_name, ' '.join(value.split()), data.get('x'), data.get('y'))
                else:
                    name = data.get('name', '') if action == 'map-goal' else '현재 위치'
                    if not isinstance(name, str) or len(name) > 24:
                        raise ValueError
                    value = (' '.join(name.split()), data.get('x'), data.get('y'))
                    action = 'goal' if action == 'map-goal' else 'position'
            except ValueError:
                self.respond(400, json.dumps({'message': '명령 또는 지도 선택 내용을 확인해 주세요.'}))
                return
            if action is None:
                self.respond(422, json.dumps({'message': '등록된 목표 이름이나 전진·후진·회전·정지 중 한 동작을 말해 주세요.'}))
                return
        elif action not in ('stop', 'forward', 'backward', 'route-step', 'route-run', *DIRECT_SETUP_ACTIONS) and action not in self.control.actions:
            self.respond(404, json.dumps({'message': '지원하지 않는 동작입니다.'}))
            return
        try:
            generation = self.headers.get('X-Trial-Generation')
            if action == 'stop':
                state = self.control.stop(command_text=text, user_command=True)
            elif action in ('distance', 'point', 'position', 'goal', 'route-target', *map_setup, *DIRECT_SETUP_ACTIONS):
                state = self.control.configure(action, value, generation, text)
            elif action == 'route-step':
                state = self.control.route_step(generation)
            elif action == 'route-run':
                state = self.control.run_route(generation)
            elif action == 'navigate':
                state = self.control.run_route(generation, value, text)
            else:
                state = self.control.start(action, generation, value, text)
        except ValueError as error:
            self.respond(409, json.dumps({'message': str(error)}))
            return
        self.respond(503 if state['status'] == 'fault' else 200, json.dumps(state))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='enable motor control on the JetBot')
    args = parser.parse_args()
    from test_move.continuous_forward import CALIBRATION_PATH, MAX_DISTANCE_CM, MAX_DURATION_S, SPEED, duration_for_cm
    from test_move.continuous_turn import MAX_DURATION_S as MAX_TURN_SECONDS
    from test_move.motor import JetbotMotor, MockMotor

    measured = [point['measured_cm'] for point in json.loads(
        CALIBRATION_PATH.read_text(encoding='utf-8')
    )['continuous_forward_points']]
    forward_durations = {}
    for distance in range(math.ceil(min(measured)), math.floor(min(max(measured), MAX_DISTANCE_CM)) + 1):
        duration = duration_for_cm(distance) * TARGET_FORWARD_CM / MEASURED_FORWARD_CM
        if math.isfinite(duration) and 0 < duration <= MAX_DURATION_S:
            forward_durations[distance] = duration
    if TARGET_FORWARD_CM not in forward_durations:
        parser.error('10cm 보정 시간이 유효하지 않습니다.')
    turn_seconds = min(MAX_TURN_SECONDS, PREVIOUS_TURN_SECONDS * TARGET_TURN_DEGREES / MEASURED_TURN_DEGREES)
    def shutdown(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    with LocalServer(('127.0.0.1', PORT), TrialHandler) as server:
        motor = JetbotMotor() if args.live else MockMotor()
        control = MotionControl(motor, SPEED, forward_durations, turn_seconds, 'live' if args.live else 'mock')
        server.RequestHandlerClass = partial(TrialHandler, control, secrets.token_hex(24))
        try:
            print('Forward {}-{}cm, 10cm {:.3f}s, left/right {:.3f}s'.format(
                min(forward_durations), max(forward_durations), forward_durations[TARGET_FORWARD_CM], turn_seconds,
            ), flush=True)
            print('JetBot {} control: http://127.0.0.1:{}/'.format(control.mode, PORT), flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            control.stop()


if __name__ == '__main__':
    main()
