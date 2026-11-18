const status = document.querySelector('#recording-status');
const detail = document.querySelector('#capture-detail');
const sideStatus = document.querySelector('#side-status');
const sideHeading = document.querySelector('#side-heading');
const history = document.querySelector('#history');
const context = { album: null, capture: null, activeSide: 'B' };
const minsec = frames => {
  const seconds = Math.floor(frames / 48000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
};
async function load() {
  const [album, capture] = await Promise.all([
    fetch('../tests/fixtures/p0/album_compilation_ab.json').then(r => r.json()),
    fetch('../tests/fixtures/p0/capture_snapshot_v1.json').then(r => r.json()),
  ]);
  context.album = album;
  context.capture = capture;
  document.querySelector('#album-title-heading').textContent = 'Example Compilation';
  document.querySelector('#album-artist').textContent = album.album_artist;
  document.querySelector('#artist-edit').value = album.album_artist;
  document.querySelector('#record-now').textContent = 'Record from now';
  document.querySelector('#save-recent').textContent = 'Save recent audio…';
  renderSide();
}
function renderSide() {
  const take = context.album.takes.find(item => item.side === context.activeSide);
  const recording = take.state === 'recording';
  sideHeading.textContent = `Side ${take.side} · ${recording ? 'recording' : 'saved'}`;
  sideStatus.textContent = recording
    ? `Recording · ${minsec(context.capture.accepted_frames)} accepted · ${minsec(context.capture.included_prefix_frames)} recent audio included. This is not a saved master.`
    : `Saved · ${take.recorded_date} · fixture history.`;
  status.textContent = recording ? `Recording side ${take.side} · ${minsec(context.capture.accepted_frames)}` : 'Input connected · waiting for sound';
  detail.textContent = `Capture state: ${context.capture.state}. Accepted frames are separate from published frames.`;
  const track = take.tracks[0];
  document.querySelector('#now-title').textContent = track.title;
  document.querySelector('#now-artist').textContent = `${track.performer} · track performer (album artist: ${context.album.album_artist})`;
  for (const button of document.querySelectorAll('[data-side]')) button.setAttribute('aria-pressed', String(button.dataset.side === take.side));
  history.replaceChildren(...context.album.takes.map(item => {
    const li = document.createElement('li');
    li.textContent = `Side ${item.side} · ${item.state} · ${item.recorded_date} · ${item.tracks.map(t => `${t.side_position}: ${t.title} — ${t.performer}`).join(', ')}`;
    return li;
  }));
  drawWaveform();
}
function drawWaveform() {
  const canvas = document.querySelector('#waveform');
  const box = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(box.width * scale));
  canvas.height = Math.floor(115 * scale);
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = '#73d4c0'; ctx.lineWidth = scale;
  ctx.beginPath();
  const samples = 160;
  for (let i = 0; i < samples; i++) {
    const x = i * canvas.width / samples;
    const amplitude = (0.08 + Math.abs(Math.sin(i * .21) * Math.cos(i * .063)) * .36) * canvas.height;
    ctx.moveTo(x, canvas.height / 2 - amplitude);
    ctx.lineTo(x, canvas.height / 2 + amplitude);
  }
  ctx.stroke();
}
document.querySelectorAll('[data-side]').forEach(button => button.addEventListener('click', () => { context.activeSide = button.dataset.side; renderSide(); }));
document.querySelector('#correct-release').addEventListener('click', () => { document.querySelector('#correction').hidden = false; document.querySelector('#album-edit').focus(); });
document.querySelector('#cancel-correction').addEventListener('click', () => { document.querySelector('#correction').hidden = true; document.querySelector('#correct-release').focus(); });
document.querySelector('#correction').addEventListener('submit', event => {
  event.preventDefault();
  document.querySelector('#album-title-heading').textContent = document.querySelector('#album-edit').value || 'Unknown album';
  document.querySelector('#album-artist').textContent = document.querySelector('#artist-edit').value || 'Unknown album artist';
  document.querySelector('#release-edition').textContent = document.querySelector('input[name="release"]:checked').value;
  document.querySelector('#correction').hidden = true;
  document.querySelector('#correct-release').focus();
});
for (const id of ['record-now', 'save-recent']) document.querySelector(`#${id}`).addEventListener('click', () => { document.querySelector('#side-status').textContent = 'Fixture preview only. No recording action was sent.'; });
window.addEventListener('resize', drawWaveform);
load().catch(() => { status.textContent = 'Fixture data unavailable'; detail.textContent = 'Open this preview from the repository root using a local web server.'; });
