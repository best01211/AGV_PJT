import { demoCameras, mounted } from './mapping-demo.js?v=agv-pose';

const canvas = document.getElementById('sensor-map');
const ctx = canvas.getContext('2d');
const raster = document.createElement('canvas');
const rasterContext = raster.getContext('2d');
const scaleLabel = document.getElementById('map-scale');
const colors = {
  unknown: [24, 39, 52, 255], free: [43, 103, 89, 255],
  uncertain: [123, 99, 55, 255], occupied: [168, 192, 201, 255],
};
let state;
let demo;
let now;
let zoom = 1;
let offsetX = 0;
let offsetY = 0;
let drag;

export function setMap(next, options) {
  if (state?.data !== next?.data || next?.revision !== setMap.revision) {
    if (next) {
      raster.width = next.cols;
      raster.height = next.rows;
      const pixels = rasterContext.createImageData(next.cols, next.rows);
      for (let row = 0; row < next.rows; row++) {
        for (let col = 0; col < next.cols; col++) {
          const value = next.data[row * next.cols + col];
          const color = value < 0 ? colors.unknown : value <= 35 ? colors.free :
            value >= 65 ? colors.occupied : colors.uncertain;
          pixels.data.set(color, ((next.rows - 1 - row) * next.cols + col) * 4);
        }
      }
      rasterContext.putImageData(pixels, 0, 0);
    }
    setMap.revision = next?.revision;
  }
  state = next;
  ({ demo, now } = options);
  draw();
}

export function fitMap() {
  zoom = 1;
  offsetX = 0;
  offsetY = 0;
  drag = undefined;
  canvas.classList.remove('dragging');
  draw();
}

function draw() {
  const { width, height } = canvas.getBoundingClientRect();
  if (!width || !height) return;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const pixelsWide = Math.round(width * ratio);
  const pixelsHigh = Math.round(height * ratio);
  if (canvas.width !== pixelsWide || canvas.height !== pixelsHigh) {
    canvas.width = pixelsWide;
    canvas.height = pixelsHigh;
  }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.fillStyle = '#101a24';
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = '#314150';
  for (let x = 10; x < width; x += 18) {
    for (let y = 10; y < height; y += 18) ctx.fillRect(x, y, 1, 1);
  }
  canvas.classList.toggle('zoomed', zoom > 1 && !!state);
  document.getElementById('zoom-in').disabled = !state || zoom >= 3.5;
  document.getElementById('zoom-out').disabled = !state || zoom <= 1;
  document.getElementById('zoom-fit').disabled = !state || zoom === 1;
  if (!state) {
    scaleLabel.textContent = '';
    return;
  }

  const c = state.config;
  const padding = width < 570 ? 19 : 44;
  const base = Math.min((width - 2 * padding) / c.width_m, (height - 68) / c.height_m);
  const unit = base * zoom;
  const w = c.width_m * unit;
  const h = c.height_m * unit;
  const boundX = Math.max(0, (w - width) / 2 + padding);
  const boundY = Math.max(0, (h - height) / 2 + 34);
  offsetX = Math.max(-boundX, Math.min(boundX, offsetX));
  offsetY = Math.max(-boundY, Math.min(boundY, offsetY));
  const left = (width - w) / 2 + offsetX;
  const top = (height - h) / 2 + offsetY;
  const point = (x, y) => [left + (x - c.origin_x_m) * unit, top + h - (y - c.origin_y_m) * unit];
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(raster, left, top, w, h);
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, top, w, h);
  ctx.clip();
  const gridSpacing = unit * Math.max(1, Math.ceil(20 / unit));
  ctx.strokeStyle = '#afcbe01a';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = left; x <= left + w; x += gridSpacing) {
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + h);
  }
  for (let y = top + h; y >= top; y -= gridSpacing) {
    ctx.moveTo(left, y);
    ctx.lineTo(left + w, y);
  }
  ctx.stroke();
  ctx.strokeStyle = '#557084';
  ctx.strokeRect(left, top, w, h);

  const pose = state.last_pose;
  const fresh = pose && (demo || now - pose.timestamp_s <= c.max_age_s);
  if (fresh && document.getElementById('camera-layer').checked) {
    for (const mount of demo ? demoCameras : c.camera_views ?? []) {
      const geometry = mounted(pose, mount);
      cone(ctx, point, unit, geometry, mount.max_range_m, mount.fov_rad, '#6ab9ff25', '#83c5fc8c', false);
    }
  }
  if (document.getElementById('range-layer').checked) {
    for (const mount of c.sensors) {
      const range = state.latest_ranges?.[mount.sensor_id];
      if (!range || range.distance_m === null || (!demo && now - range.timestamp_s > c.max_age_s)) continue;
      const geometry = mounted(range.pose, mount);
      cone(ctx, point, unit, geometry, range.distance_m, mount.fov_rad, '#ffb65734', '#ffba65', true);
    }
  }

  const trail = state.pose_trail ?? [];
  if (trail.length) {
    ctx.beginPath();
    trail.forEach((entry, i) => {
      const location = point(entry.x_m, entry.y_m);
      if (i === 0 || entry.break_before) ctx.moveTo(...location);
      else ctx.lineTo(...location);
    });
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.lineWidth = 4;
    ctx.strokeStyle = '#0e5147';
    ctx.stroke();
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = '#9bf4d3';
    ctx.stroke();
    const start = trail[0];
    const [x, y] = point(start.x_m, start.y_m);
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, 2 * Math.PI);
    ctx.fillStyle = '#dafff1';
    ctx.fill();
  }
  if (pose) robot(ctx, point(pose.x_m, pose.y_m), unit, pose.yaw_rad, !fresh);
  ctx.restore();

  const targetScale = 57 / unit;
  const decade = 10 ** Math.floor(Math.log10(targetScale));
  const meters = ([5, 2, 1].map(n => n * decade).find(n => n <= targetScale) ?? decade);
  scaleLabel.textContent = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 3 }).format(meters) + ' m';
  scaleLabel.style.setProperty('--scale-width', (meters * unit) + 'px');
}

function cone(ctx, point, unit, geometry, range, fov, fill, stroke, echo) {
  const [x, y] = point(geometry.x, geometry.y);
  const middle = -geometry.heading;
  const start = middle - fov / 2;
  const end = middle + fov / 2;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.arc(x, y, range * unit, start, end);
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 1;
  ctx.setLineDash(echo ? [2, 4] : [5, 4]);
  ctx.stroke();
  ctx.setLineDash([]);
  if (echo) {
    ctx.beginPath();
    ctx.arc(x, y, range * unit, start, end);
    ctx.lineWidth = 1.8;
    ctx.stroke();
  }
}

function robot(ctx, [x, y], unit, heading, stale) {
  agvPose.draw(ctx, x, y, unit, heading, stale);
  const label = (stale ? '마지막 위치' : 'AGV-01') + ' · ' + agvPose.heading(heading);
  ctx.font = '700 11px system-ui';
  const labelWidth = ctx.measureText(label).width + 12;
  ctx.fillStyle = '#10202c';
  ctx.fillRect(x - labelWidth / 2, y + 15, labelWidth, 19);
  ctx.fillStyle = '#e1fff3';
  ctx.textAlign = 'center';
  ctx.fillText(label, x, y + 28);
}

new ResizeObserver(draw).observe(canvas);
document.getElementById('zoom-in').onclick = () => { zoom = Math.min(3.5, zoom * 1.35); draw(); };
document.getElementById('zoom-out').onclick = () => {
  zoom = Math.max(1, zoom / 1.35);
  if (zoom <= 1.01) fitMap();
  else draw();
};
document.getElementById('zoom-fit').onclick = fitMap;
for (const id of ['range-layer', 'camera-layer']) document.getElementById(id).onchange = draw;
canvas.addEventListener('pointerdown', event => {
  if (event.button !== 0 || zoom <= 1 || !state) return;
  drag = { id: event.pointerId, x: event.clientX - offsetX, y: event.clientY - offsetY };
  canvas.setPointerCapture(event.pointerId);
  canvas.classList.add('dragging');
});
canvas.addEventListener('pointermove', event => {
  if (drag?.id !== event.pointerId) return;
  offsetX = event.clientX - drag.x;
  offsetY = event.clientY - drag.y;
  draw();
});
canvas.addEventListener('lostpointercapture', event => {
  if (drag?.id !== event.pointerId) return;
  drag = undefined;
  canvas.classList.remove('dragging');
});
