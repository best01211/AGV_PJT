const agvPose = {
  widthM: 0.20,
  lengthM: 0.20,

  heading(yaw) {
    if (!Number.isFinite(yaw)) return '—';
    const degrees = Math.round(90 - yaw * 180 / Math.PI);
    return ((degrees % 360 + 360) % 360) + '°';
  },

  draw(context, x, y, pixelsPerMeter, yaw, stale = false) {
    const halfWidth = this.widthM * pixelsPerMeter / 2;
    const halfLength = this.lengthM * pixelsPerMeter / 2;
    const tip = Math.max(12, halfLength + 5);
    context.save();
    context.translate(x, y);
    context.rotate(-yaw);
    context.fillStyle = stale ? '#bcc8d2' : '#a6ffdf';
    context.strokeStyle = '#12343a';
    context.lineWidth = 1.5;
    context.fillRect(-halfLength, -halfWidth, halfLength * 2, halfWidth * 2);
    context.strokeRect(-halfLength, -halfWidth, halfLength * 2, halfWidth * 2);
    context.beginPath();
    context.moveTo(tip, 0);
    context.lineTo(tip - 7, -4);
    context.lineTo(tip - 7, 4);
    context.closePath();
    context.fill();
    context.stroke();
    context.restore();
  },
};
