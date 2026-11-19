(() => {
  let cols, rows, data, start;
  const destinations = new Map();
  const editor = $('map-editor');
  const grid = $('editor-grid');
  const feedback = $('editor-result');
  const tool = $('grid-tool');

  function renderGrid() {
    grid.style.gridTemplateColumns = `repeat(${cols}, var(--cell-size))`;
    const namesByCell = new Map();
    for (const {name, index} of destinations.values()) {
      const names = namesByCell.get(index) ?? [];
      names.push(name);
      namesByCell.set(index, names);
    }
    const cells = [];
    for (let row = rows - 1; row >= 0; row--) {
      for (let col = 0; col < cols; col++) {
        const index = row * cols + col;
        const names = namesByCell.get(index) ?? [];
        const marks = [index === start ? '시작' : '', ...names].filter(Boolean);
        const cell = document.createElement('button');
        cell.type = 'button';
        cell.dataset.index = index;
        cell.dataset.kind = data[index] === 100 ? 'wall' : data[index] === -1 ? 'unknown' : 'free';
        const kind = {wall:'장애물', unknown:'미확인', free:'빈 칸'}[cell.dataset.kind];
        const description = `[${col}, ${row}] ${kind} ${marks.join(', ')}`.trim();
        cell.setAttribute('aria-label', description);
        cell.title = description;
        cell.textContent = marks.join(' · ');
        cell.onclick = () => editCell(index);
        cells.push(cell);
      }
    }
    grid.replaceChildren(...cells);
  }

  function resetGrid() {
    cols = $('grid-cols').valueAsNumber;
    rows = $('grid-rows').valueAsNumber;
    data = Array(cols * rows).fill(0);
    start = null;
    destinations.clear();
    feedback.textContent = '';
    renderGrid();
  }

  function editCell(index) {
    const action = tool.value;
    if (action === 'start' || action === 'destination') {
      if (data[index] !== 0) {
        feedback.textContent = '시작점과 목적지는 빈 칸에만 지정할 수 있습니다.';
        return;
      }
      if (action === 'start') {
        start = index;
      } else {
        const name = $('grid-destination').value.trim().replace(/ +/g, ' ');
        if (!/^[A-Za-z0-9가-힣]+(?: [A-Za-z0-9가-힣]+)*$/.test(name)) {
          feedback.textContent = '목적지 이름에 한글, 영문, 숫자, 공백을 사용해 주세요.';
          $('grid-destination').focus();
          return;
        }
        destinations.set(name.toLowerCase(), {name, index});
      }
    } else {
      data[index] = {wall:100, unknown:-1, free:0}[action];
      if (start === index) start = null;
      for (const [key, destination] of destinations) {
        if (destination.index === index) destinations.delete(key);
      }
    }
    feedback.textContent = '';
    renderGrid();
    grid.querySelector(`[data-index="${index}"]`).focus();
  }

  $('create-map').onclick = () => {
    editor.hidden = !editor.hidden;
    $('create-map').setAttribute('aria-expanded', String(!editor.hidden));
    if (!editor.hidden && !data) resetGrid();
  };
  $('grid-form').onsubmit = event => {
    event.preventDefault();
    resetGrid();
  };
  tool.onchange = () => {
    $('grid-destination-field').hidden = tool.value !== 'destination';
  };

  $('register-map').onclick = async () => {
    if (start === null || !destinations.size) {
      feedback.textContent = '로봇 시작점과 목적지를 하나 이상 지정해 주세요.';
      return;
    }
    const frame_id = $('grid-name').value.trim();
    const resolution = $('grid-resolution');
    const heading = $('grid-start-heading');
    if (!frame_id || !Number.isFinite(resolution.valueAsNumber) || !resolution.checkValidity()) {
      feedback.textContent = '지도 이름과 한 칸 크기(0.02~1m)를 입력해 주세요.';
      return;
    }
    if (!heading.reportValidity()) return;
    const cell = index => [index % cols, Math.floor(index / cols)];
    const map = {
      schema_version:1, frame_id, resolution_m:resolution.valueAsNumber, cols, rows,
      data:[...data], start:cell(start), start_heading_deg:heading.valueAsNumber,
      destinations:Object.fromEntries([...destinations.values()].map(({name, index}) => [name, cell(index)]))
    };
    if (await loadMap(() => map)) {
      editor.hidden = true;
      $('create-map').setAttribute('aria-expanded', 'false');
      $('command').focus();
    } else {
      feedback.textContent = $('map-result').textContent;
    }
  };
})();
