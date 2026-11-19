export const directions = ['front', 'rear', 'left', 'right'];
export const demoCameras = [
  { x_m: .10, y_m: .07, yaw_rad: .09, fov_rad: .86, max_range_m: 3.2 },
  { x_m: .10, y_m: -.07, yaw_rad: -.09, fov_rad: .86, max_range_m: 3.2 },
];

const cols = 66;
const rows = 43;
const resolution = .2;
const config = {
  frame_id: 'ROVE · 실내 테스트 구역',
  width_m: cols * resolution, height_m: rows * resolution,
  resolution_m: resolution, origin_x_m: 0, origin_y_m: 0,
  sensors: directions.map((sensor_id, i) => {
    const yaw_rad = [0, Math.PI, Math.PI / 2, -Math.PI / 2][i];
    return { sensor_id, yaw_rad, x_m: .10 * Math.cos(yaw_rad), y_m: .10 * Math.sin(yaw_rad),
      fov_rad: .5, max_range_m: 2.8 };
  }),
};

const floor = new Uint8Array(cols * rows);
const inside = (x, y) => x >= 0 && x < config.width_m && y >= 0 && y < config.height_m;
const cellAt = (x, y) => Math.floor(y / resolution) * cols + Math.floor(x / resolution);
function rectangle(x, y, w, h, value) {
  for (let row = y; row < y + h; row++) floor.fill(value, row * cols + x, row * cols + x + w);
}
rectangle(2, 2, 62, 39, 2);
rectangle(3, 3, 60, 37, 1);
rectangle(3, 30, 19, 1, 2);
rectangle(22, 34, 1, 6, 2);
rectangle(43, 32, 20, 1, 2);
rectangle(43, 33, 1, 3, 2);
rectangle(11, 8, 3, 17, 2);
rectangle(22, 8, 3, 17, 2);
rectangle(33, 8, 3, 17, 2);
rectangle(44, 8, 3, 17, 2);
rectangle(55, 8, 3, 17, 2);
rectangle(29, 35, 8, 3, 2);
rectangle(52, 35, 8, 3, 2);

const route = [[6, 6], [6, 27], [17, 27], [17, 6], [28, 6], [28, 27],
  [39, 27], [39, 6], [50, 6], [50, 27], [60, 27], [60, 6], [60, 28],
  [40, 28], [40, 37], [47, 37], [47, 36], [47, 37], [40, 37], [40, 32], [18, 32],
  [18, 37], [7, 37]].map(point => point.map(cell => (cell + .5) * resolution));
const turnRadius = Math.hypot(agvPose.widthM, agvPose.lengthM) / 2;

function canSweep([ax, ay], [bx, by]) {
  const x0 = Math.min(ax, bx);
  const x1 = Math.max(ax, bx);
  const y0 = Math.min(ay, by);
  const y1 = Math.max(ay, by);
  if (!inside(x0 - turnRadius, y0 - turnRadius) || !inside(x1 + turnRadius, y1 + turnRadius)) return false;
  for (let row = Math.ceil((y0 - turnRadius) / resolution) - 1;
    row <= Math.floor((y1 + turnRadius) / resolution); row++) {
    for (let col = Math.ceil((x0 - turnRadius) / resolution) - 1;
      col <= Math.floor((x1 + turnRadius) / resolution); col++) {
      if (floor[row * cols + col] === 1) continue;
      const dx = Math.max(col * resolution - x1, x0 - (col + 1) * resolution, 0);
      const dy = Math.max(row * resolution - y1, y0 - (row + 1) * resolution, 0);
      if (Math.hypot(dx, dy) <= turnRadius) return false;
    }
  }
  return true;
}

const samples = [];
let routeBlocked = false;
for (let i = 1; i < route.length; i++) {
  if (!canSweep(route[i - 1], route[i])) {
    routeBlocked = true;
    break;
  }
  const [x, y] = route[i - 1];
  const dx = route[i][0] - x;
  const dy = route[i][1] - y;
  const count = Math.ceil(Math.hypot(dx, dy) / .16);
  const heading = Math.atan2(dy, dx);
  const previous = samples.at(-1)?.yaw_rad ?? heading;
  const turn = Math.atan2(Math.sin(heading - previous), Math.cos(heading - previous));
  const turnSteps = Math.ceil(Math.abs(turn) / (Math.PI / 12));
  if (!samples.length) samples.push({ x_m: x, y_m: y, yaw_rad: heading });
  for (let step = 1; step <= turnSteps; step++) {
    samples.push({ x_m: x, y_m: y, yaw_rad: previous + turn * step / turnSteps });
  }
  for (let step = 1; step <= count; step++) {
    samples.push({ x_m: x + dx * step / count, y_m: y + dy * step / count, yaw_rad: heading });
  }
}

export function mounted(pose, mount) {
  const cosine = Math.cos(pose.yaw_rad);
  const sine = Math.sin(pose.yaw_rad);
  return {
    x: pose.x_m + cosine * mount.x_m - sine * mount.y_m,
    y: pose.y_m + sine * mount.x_m + cosine * mount.y_m,
    heading: pose.yaw_rad + mount.yaw_rad,
  };
}

export class ExplorationDemo {
  constructor() {
    this.reset();
    this.advance(Math.floor(samples.length * .46));
  }

  reset() {
    this.state = { config, cols, rows, data: new Int16Array(floor.length).fill(-1),
      revision: 0, accepted: 0, pose_trail: [], latest_ranges: {}, last_pose: null,
      last_update_s: null };
    this.step = 0;
  }

  get finished() { return this.step === samples.length; }
  get blocked() { return this.finished && routeBlocked; }

  advance(count = 1) {
    while (this.step < samples.length && count-- > 0) {
      const pose = samples[this.step++];
      const state = this.state;
      state.pose_trail.push(pose);
      state.last_pose = pose;
      state.last_update_s = Date.now() / 1000;
      state.revision++;
      for (const sensor of config.sensors) {
        const distance = this.scan(pose, sensor, 11, true);
        state.latest_ranges[sensor.sensor_id] = { distance_m: distance, pose };
        if (distance !== null) state.accepted++;
      }
      for (const camera of demoCameras) this.scan(pose, camera, 23, false);
    }
  }

  scan(pose, mount, rayCount, ultrasound) {
    const { x, y, heading } = mounted(pose, mount);
    let closest = null;
    for (let ray = 0; ray < rayCount; ray++) {
      const angle = heading + mount.fov_rad * (ray / (rayCount - 1) - .5);
      for (let radius = 0; radius <= mount.max_range_m; radius += resolution / 3) {
        const sx = x + radius * Math.cos(angle);
        const sy = y + radius * Math.sin(angle);
        if (!inside(sx, sy)) break;
        const i = cellAt(sx, sy);
        if (floor[i] !== 1) {
          if (floor[i] === 2) {
            if (ultrasound) closest = Math.min(closest ?? Infinity, radius);
            this.state.data[i] = ultrasound && this.state.data[i] < 65 ? 53 : 95;
          }
          break;
        }
        this.state.data[i] = 12;
      }
    }
    return closest;
  }
}
