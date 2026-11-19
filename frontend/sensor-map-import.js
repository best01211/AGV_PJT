(() => {
  const disclosure = $('sensor-map-import');
  const setupForm = $('sensor-preview-form');
  const pointForm = $('sensor-point-form');
  const previewStatus = $('sensor-preview-status');
  const pointStatus = $('sensor-point-status');
  const registerStatus = $('sensor-register-status');
  const canvas = $('sensor-navigation-canvas');
  const context = canvas.getContext('2d');
  const raster = document.createElement('canvas');
  const colors = {
    '-1': [24, 39, 52, 255], 0: [55, 122, 102, 255], 50: [136, 115, 73, 255],
    75: [96, 89, 133, 255], 100: [168, 192, 201, 255],
  };
  const xInput = $('sensor-x');
  const yInput = $('sensor-y');
  const kind = $('sensor-point-kind');
  const nameInput = $('sensor-point-name');
  const startHeading = $('sensor-start-heading');
  const registerButton = $('sensor-register');
  const destinations = new Map();
  let source;
  let map;
  let start;
  let startPosition;
  let pending = false;
  let registered = false;

  function setup() {
    return {
      id: $('sensor-map-id').value.trim().toLowerCase(),
      footprint: {
        margin_m: $('sensor-margin').valueAsNumber,
      },
    };
  }

  function matchesPreview() {
    return source && JSON.stringify(source) === JSON.stringify(setup());
  }

  function post(action, id, payload) {
    return request('/api/navigation/sensor-maps/' + id + '/' + action, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
  }

  function fit() {
    const size = Math.min((canvas.width - 32) / map.cols, (canvas.height - 32) / map.rows);
    return { size, left: (canvas.width - map.cols * size) / 2, top: (canvas.height - map.rows * size) / 2 };
  }

  function draw() {
    if (!map) return;
    const { size, left, top } = fit();
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.imageSmoothingEnabled = false;
    context.drawImage(raster, left, top, map.cols * size, map.rows * size);
    context.lineWidth = 1;
    context.strokeStyle = '#89a7ba65';
    context.strokeRect(left, top, map.cols * size, map.rows * size);
    const [col, row] = mapCellAt(map, inputPosition());
    if (inMap(col, row)) {
      context.strokeStyle = '#fff4d4';
      context.lineWidth = 2;
      context.strokeRect(left + col * size, top + (map.rows - row - 1) * size, size, size);
    }

    function marker([x, y], label, fill) {
      const screenX = left + x * size;
      const screenY = top + (map.rows - y) * size;
      context.beginPath();
      context.arc(screenX, screenY, 11, 0, Math.PI * 2);
      context.fillStyle = fill;
      context.fill();
      context.lineWidth = 2;
      context.strokeStyle = '#10232e';
      context.stroke();
      context.font = '700 12px system-ui';
      context.textAlign = 'center';
      context.textBaseline = 'middle';
      context.fillStyle = '#10232e';
      context.fillText(label, screenX, screenY);
    }

    let index = 1;
    for (const { cell } of destinations.values()) marker(cell.map(value => value + .5), String(index++), '#b9deff');
    if (startPosition) marker([
      (startPosition[0] - map.origin_x_m) / map.resolution_m,
      (startPosition[1] - map.origin_y_m) / map.resolution_m,
    ], 'S', '#a7f1d1');
  }

  function drawGrid() {
    raster.width = map.cols;
    raster.height = map.rows;
    const rasterContext = raster.getContext('2d');
    const image = rasterContext.createImageData(map.cols, map.rows);
    map.data.forEach((cell, index) => {
      const target = (map.rows - 1 - Math.floor(index / map.cols)) * map.cols + index % map.cols;
      image.data.set(colors[cell], target * 4);
    });
    rasterContext.putImageData(image, 0, 0);
  }

  function inMap(col, row) {
    return Number.isInteger(col) && Number.isInteger(row) && col >= 0 && row >= 0
      && col < map.cols && row < map.rows;
  }

  function inputPosition() {
    return [xInput.valueAsNumber, yInput.valueAsNumber];
  }

  function updateButtons() {
    const editable = map && matchesPreview() && Boolean(map.driveable_cells);
    setupForm.querySelector('fieldset').disabled = pending;
    pointForm.querySelector('fieldset').disabled = pending || !editable;
    startHeading.disabled = pending || !editable;
    registerButton.disabled = pending || !editable || !start || !destinations.size || registered;
    $('sensor-drive-link').hidden = !registered;
  }

  function renderPoints() {
    $('sensor-start-label').textContent = startPosition ? '시작점 S · ' + positionLabel(startPosition) : '시작점 미지정';
    const items = [];
    for (const [key, { name, cell }] of destinations) {
      const item = document.createElement('li');
      const label = document.createElement('span');
      const center = mapPositionAt(map, cell[0] + .5, cell[1] + .5).map(value => Number(value.toFixed(6)));
      label.textContent = (items.length + 1) + '. ' + name + ' · ' + positionLabel(center);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '제거';
      remove.setAttribute('aria-label', name + ' 제거');
      remove.disabled = pending || !matchesPreview();
      remove.onclick = () => {
        if (pending || !matchesPreview()) return;
        destinations.delete(key);
        registered = false;
        registerStatus.textContent = '';
        renderPoints();
      };
      item.append(label, remove);
      items.push(item);
    }
    $('sensor-destinations').replaceChildren(...items);
    draw();
    updateButtons();
  }

  setupForm.oninput = () => {
    if (!map) return;
    previewStatus.textContent = matchesPreview() ? '' : '입력값이 바뀌었습니다. 다시 불러와 변환 결과를 확인하세요.';
    renderPoints();
  };
  startHeading.oninput = () => {
    registered = false;
    registerStatus.textContent = '';
    updateButtons();
  };

  setupForm.onsubmit = async event => {
    event.preventDefault();
    if (pending) return;
    const next = setup();
    pending = true;
    updateButtons();
    previewStatus.textContent = '저장된 관측 지도를 변환하는 중입니다.';
    try {
      const preview = await post('preview', next.id, next.footprint);
      source = next;
      map = preview;
      start = startPosition = undefined;
      destinations.clear();
      registered = false;
      xInput.value = yInput.value = '';
      pointStatus.textContent = registerStatus.textContent = '';
      $('sensor-navigation-draft').hidden = false;
      $('sensor-preview-stats').textContent = map.frame_id + ' · ' + map.cols + ' × ' + map.rows
        + ' 셀 · 통행 가능 ' + map.driveable_cells.toLocaleString('ko-KR')
        + '셀 / 자유 증거 ' + map.free_evidence_cells.toLocaleString('ko-KR')
        + '셀 · 외형과 여유 반경 ' + map.clearance_radius_m.toFixed(3)
        + ' m (' + map.clearance_cells + '칸 차단 기준)';
      previewStatus.textContent = map.driveable_cells ? '변환했습니다. 시작점과 목적지를 지정하세요.'
          : map.free_evidence_cells ? '20 × 20cm 외형과 여유 공간을 적용한 뒤 통행 가능한 칸이 없습니다. 관측 범위와 추가 여유를 확인하세요.'
          : map.free_updates_enabled ? '자유 공간 증거가 충분하지 않습니다. 관측을 더 수집하고 다시 저장하세요.'
            : '이 저장 지도는 자유 공간 갱신이 꺼져 있고 자유 증거가 없습니다. 검증된 설정으로 관측을 새로 수집하세요.';
      drawGrid();
    } catch (error) {
      previewStatus.textContent = error.message;
    } finally {
      pending = false;
      renderPoints();
    }
  };

  kind.onchange = () => {
    const destination = kind.value === 'destination';
    $('sensor-point-name-field').hidden = !destination;
    nameInput.required = destination;
    $('sensor-set-point').textContent = destination ? '목적지 추가' : '시작점 지정';
  };
  xInput.oninput = yInput.oninput = draw;
  canvas.onclick = event => {
    if (!map || pending || !matchesPreview() || !map.driveable_cells) return;
    const box = canvas.getBoundingClientRect();
    const { size, left, top } = fit();
    const gridX = ((event.clientX - box.left) * canvas.width / box.width - left) / size;
    const gridY = map.rows - ((event.clientY - box.top) * canvas.height / box.height - top) / size;
    const position = mapPositionAt(map, gridX, gridY).map(value => Number(value.toFixed(4)));
    const [col, row] = mapCellAt(map, position);
    if (!inMap(col, row)) return;
    [xInput.value, yInput.value] = position;
    pointStatus.textContent = map.data[row * map.cols + col] === 0
      ? '선택한 좌표 ' + positionLabel(position)
      : '이 좌표에서는 통행할 수 없습니다.';
    draw();
  };

  pointForm.onsubmit = event => {
    event.preventDefault();
    if (pending || !matchesPreview()) return;
    const position = inputPosition();
    const [col, row] = mapCellAt(map, position);
    if (!inMap(col, row) || map.data[row * map.cols + col] !== 0) {
      pointStatus.textContent = '변환 지도에서 통행 가능한 칸을 선택하세요.';
      return;
    }
    if (kind.value === 'start') {
      start = [col, row];
      startPosition = position;
      pointStatus.textContent = '시작점을 지정했습니다.';
      kind.value = 'destination';
      kind.onchange();
    } else {
      const name = nameInput.value.trim().replace(/\s+/g, ' ');
      if (!/^[A-Za-z0-9가-힣]+(?: [A-Za-z0-9가-힣]+)*$/.test(name)) {
        pointStatus.textContent = '이름에는 한글, 영문, 숫자, 공백을 사용할 수 있습니다.';
        return;
      }
      const key = name.toLowerCase();
      if (destinations.has(key) || destinations.size >= 50) {
        pointStatus.textContent = '서로 다른 목적지를 최대 50개 지정할 수 있습니다.';
        return;
      }
      destinations.set(key, { name, cell: [col, row] });
      nameInput.value = '';
      pointStatus.textContent = name + ' 목적지를 추가했습니다.';
    }
    registered = false;
    registerStatus.textContent = '';
    renderPoints();
  };

  registerButton.onclick = async () => {
    if (registerButton.disabled || !startHeading.reportValidity()) return;
    const start_heading_deg = startHeading.valueAsNumber;
    pending = true;
    renderPoints();
    registerStatus.textContent = '주행 지도를 등록하는 중입니다.';
    try {
      const points = Object.fromEntries([...destinations.values()].map(({ name, cell }) => [name, cell]));
      await replaceNavigationMap(() => post('register', source.id, {
        ...source.footprint, start, start_position_m: startPosition, start_heading_deg, destinations: points,
      }));
      registered = true;
      registerStatus.textContent = '등록했습니다. 주행 설정에서 목적지를 선택해 모의 주행을 시작하세요.';
    } catch (error) {
      registerStatus.textContent = error.message;
    } finally {
      pending = false;
      renderPoints();
    }
  };

  function openLinkedImport() {
    if (location.hash === '#sensor-map-import') disclosure.open = true;
  }
  const linkedId = new URLSearchParams(location.search).get('sensor_map');
  if (/^[a-fA-F0-9]{32}$/.test(linkedId ?? '')) $('sensor-map-id').value = linkedId.toLowerCase();
  window.addEventListener('hashchange', openLinkedImport);
  openLinkedImport();
})();
