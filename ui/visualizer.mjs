const dialog = document.querySelector('#visualizer-dialog');
const canvas = document.querySelector('#visualizer-canvas');
const context = canvas.getContext('2d', {alpha: false});
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const packets = [];
let animation = 0;
let lastPaint = 0;
let preset = 'orbit';
let delayMs = 0;

function unpack(value) {
  if (typeof value !== 'string' || value.length > 4200) return null;
  try {
    const raw = atob(value);
    if (raw.length !== 1024) return null;
    return Uint8Array.from(raw, char => char.charCodeAt(0));
  } catch { return null; }
}

function decode(frame) {
  const left = unpack(frame?.timeByteArrayL);
  const right = unpack(frame?.timeByteArrayR);
  if (!left || !right) return null;
  const mono = new Float32Array(1024);
  for (let i = 0; i < mono.length; i++) mono[i] = ((left[i] + right[i]) / 2 - 128) / 128;
  const real = mono.slice();
  const imag = new Float32Array(1024);
  for (let i = 1, j = 0; i < 1024; i++) {
    let bit = 512;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { [real[i], real[j]] = [real[j], real[i]]; }
  }
  for (let size = 2; size <= 1024; size <<= 1) {
    const half = size >> 1;
    const step = -2 * Math.PI / size;
    for (let start = 0; start < 1024; start += size) {
      for (let i = 0; i < half; i++) {
        const angle = step * i;
        const wr = Math.cos(angle), wi = Math.sin(angle);
        const even = start + i, odd = even + half;
        const tr = wr * real[odd] - wi * imag[odd];
        const ti = wr * imag[odd] + wi * real[odd];
        real[odd] = real[even] - tr; imag[odd] = imag[even] - ti;
        real[even] += tr; imag[even] += ti;
      }
    }
  }
  const bands = new Float32Array(48);
  for (let band = 0; band < bands.length; band++) {
    const start = Math.max(1, Math.floor(2 ** (band / 6)));
    const end = Math.max(start + 1, Math.floor(2 ** ((band + 1) / 6)));
    let peak = 0;
    for (let bin = start; bin < end; bin++) {
      const magnitude = Math.hypot(real[bin], imag[bin]) / 512;
      peak = Math.max(peak, magnitude);
    }
    bands[band] = Math.min(1, Math.sqrt(peak) * 2.5);
  }
  return {bands, mono, at: performance.now()};
}

function selectedFrame(now) {
  const target = now - delayMs;
  let selected = null;
  for (const frame of packets) {
    if (frame.at <= target) selected = frame;
    else break;
  }
  return selected;
}

function sizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(1.5, window.devicePixelRatio || 1);
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width; canvas.height = height;
  }
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {width: rect.width, height: rect.height};
}

function paint(frame, now, animated) {
  const {width, height} = sizeCanvas();
  context.fillStyle = '#08090d';
  context.fillRect(0, 0, width, height);
  if (!frame) {
    context.fillStyle = '#f6e7ce'; context.font = '16px system-ui';
    context.fillText('Waiting for an active live view…', 24, 40);
    return;
  }
  const bars = frame.bands;
  const maxHeight = height * 0.68;
  const gap = Math.max(2, width * 0.003);
  const barWidth = Math.max(1, (width - gap * (bars.length - 1)) / bars.length);
  const t = animated ? now / 1500 : 0;
  context.lineWidth = Math.max(1, width * .0015);
  context.strokeStyle = '#f4bb75';
  context.fillStyle = '#73d4c0';
  if (preset === 'radial') {
    const cx = width / 2, cy = height / 2;
    const base = Math.min(width, height) * .13;
    for (let i = 0; i < bars.length; i++) {
      const angle = i * Math.PI * 2 / bars.length + (animated ? t * .15 : 0);
      const radius = base + bars[i] * Math.min(width, height) * .24;
      context.beginPath(); context.moveTo(cx + Math.cos(angle) * base, cy + Math.sin(angle) * base);
      context.lineTo(cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius); context.stroke();
    }
    return;
  }
  if (preset === 'ribbon') {
    // A mirrored time-domain ribbon makes this preset visibly distinct from
    // the bar-based Orbit and spoke-based Radial renderers.
    const count = 192, center = height * .52;
    const upper = [], lower = [];
    for (let i = 0; i < count; i++) {
      const x = i * width / (count - 1);
      const sample = frame.mono[Math.floor(i * (frame.mono.length - 1) / (count - 1))];
      const flow = animated ? Math.sin(i * .045 + t) * .025 : 0;
      const amplitude = Math.max(-1, Math.min(1, sample + flow)) * height * .36;
      upper.push([x, center - amplitude]); lower.push([x, center + amplitude]);
    }
    context.beginPath();
    context.moveTo(upper[0][0], upper[0][1]);
    for (let i = 1; i < upper.length; i++) context.lineTo(upper[i][0], upper[i][1]);
    for (let i = lower.length - 1; i >= 0; i--) context.lineTo(lower[i][0], lower[i][1]);
    context.closePath();
    const ribbon = context.createLinearGradient(0, center - height * .36, 0, center + height * .36);
    ribbon.addColorStop(0, 'rgba(115,212,192,.16)');
    ribbon.addColorStop(.5, 'rgba(244,187,117,.5)');
    ribbon.addColorStop(1, 'rgba(115,212,192,.16)');
    context.fillStyle = ribbon; context.fill();
    context.lineWidth = Math.max(2, width * .0025);
    context.strokeStyle = '#73d4c0'; context.stroke();
    return;
  }
  for (let i = 0; i < bars.length; i++) {
    const level = bars[i];
    const h = Math.max(2, level * maxHeight * (preset === 'ribbon' ? 0.75 + .25 * Math.sin(i * .18 + t) : 1));
    const x = i * (barWidth + gap);
    const y = height * .82 - h;
    context.globalAlpha = .58 + level * .4;
    context.fillRect(x, y, barWidth, h);
    if (animated && preset === 'orbit') {
      const swell = Math.sin(i * .2 + t) * 3;
      context.beginPath(); context.ellipse(width / 2, height / 2, width * .12 + i * width / 130, height * .08 + i * height / 150 + swell, t * .04, 0, Math.PI * 2); context.stroke();
    }
  }
  context.globalAlpha = 1;
}

function loop(now) {
  if (!dialog.open || document.hidden || reducedMotion.matches) { animation = 0; return; }
  if (now - lastPaint >= 1000 / 24) {
    paint(selectedFrame(now), now, true);
    lastPaint = now;
  }
  animation = requestAnimationFrame(loop);
}

function start() {
  if (!dialog.open || document.hidden || reducedMotion.matches || animation) return;
  animation = requestAnimationFrame(loop);
}

export function receiveVisualization(frame, mode = 'waveform', delay = 0) {
  const decoded = decode(frame);
  if (!decoded) return null;
  packets.push(decoded);
  while (packets.length > 32 || (packets.length && decoded.at - packets[0].at > 4000)) packets.shift();
  delayMs = Math.max(0, Math.min(3000, Number(delay) || 0));
  if (mode === 'spectrum' && !reducedMotion.matches) paint(selectedFrame(performance.now()), performance.now(), false);
  if (dialog.open && reducedMotion.matches) paint(selectedFrame(performance.now()), performance.now(), false);
  return decoded.bands;
}

export function clearVisualization() { packets.length = 0; }

document.querySelector('#visualizer-preset').addEventListener('change', event => { preset = event.target.value; });
document.querySelector('#open-visualizer').addEventListener('click', () => {
  if (reducedMotion.matches) return;
  dialog.showModal();
  paint(selectedFrame(performance.now()), performance.now(), false);
  start();
});
document.querySelector('#close-visualizer').addEventListener('click', () => dialog.close());
dialog.addEventListener('close', () => { if (animation) cancelAnimationFrame(animation); animation = 0; });
document.addEventListener('visibilitychange', () => {
  if (document.hidden && animation) cancelAnimationFrame(animation);
  animation = 0;
  if (!document.hidden) start();
});
reducedMotion.addEventListener('change', () => {
  if (reducedMotion.matches && dialog.open) dialog.close();
  if (reducedMotion.matches && animation) cancelAnimationFrame(animation);
  animation = 0;
  if (!reducedMotion.matches) start();
});
window.addEventListener('resize', () => { if (dialog.open) paint(selectedFrame(performance.now()), performance.now(), !reducedMotion.matches); });
