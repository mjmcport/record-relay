import {clearVisualization, receiveVisualization} from './visualizer.mjs';

const token = document.querySelector('meta[name="control-token"]').content;
const message = document.querySelector('#message');
const captureStatus = document.querySelector('#capture-status');
const detail = document.querySelector('#capture-detail');
const sideDetail = document.querySelector('#side-detail');
const label = document.querySelector('#album-label');
const recordButton = document.querySelector('#record-now');
const bufferButton = document.querySelector('#save-recent');
const stopButton = document.querySelector('#stop-side');
const projectSelect = document.querySelector('#project-select');
const liveToggle = document.querySelector('#live-view');
const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
const visualMode = document.querySelector('#visual-mode');
let state = {projects: [], history: {items: []}, capture: {recording: 'unavailable'}};
let project = null;
let side = 'A';
let active = null;
let pinRevision = 0;
let selectedRelease = null;
let previousCaptureStatus = '';
let rangeGeneration = null;
let latestCapture = null;
let latestSpectrum = null;
let lastSpectrumAnnouncement = 0;
let pollTimer = null;
let lastAlbumRefresh = 0;
let lastVisualSequence = 0;
let captureRefreshPending = false;
let visualClientId = sessionStorage.getItem('record-relay-visual-client');
if (!visualClientId) {
  visualClientId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  sessionStorage.setItem('record-relay-visual-client', visualClientId);
}

function applyMotionPreference(reduced) {
  visualMode.options[0].disabled = reduced;
  if (reduced) visualMode.value = 'spectrum';
  document.querySelector('#motion-note').hidden = !reduced;
  document.querySelector('#open-visualizer').disabled = reduced || !liveToggle.checked;
}
applyMotionPreference(reducedMotionQuery.matches);

async function request(path, {method = 'GET', body} = {}) {
  const response = await fetch(path, {
    method,
    headers: { ...(body ? {'Content-Type': 'application/json'} : {}), ...(method === 'GET' ? {} : {'X-Control-Token': token}) },
    ...(body ? {body: JSON.stringify(body)} : {}),
    credentials: 'same-origin',
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}

function say(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

function drawLiveView(samples) {
  const canvas = document.querySelector('#live-canvas');
  const context = canvas.getContext('2d');
  const width = Math.max(1, canvas.clientWidth);
  const height = Math.max(1, canvas.clientHeight);
  const ratio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.strokeStyle = '#f4bb75';
  context.lineWidth = 2;
  context.beginPath();
  if (Array.isArray(samples) && samples.length) {
    samples.forEach((sample, index) => {
      const value = Math.max(0, Math.min(1, Number(sample) || 0));
      const x = index * width / (samples.length - 1);
      const y = height / 2 - (value * height * .44);
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
  } else {
    context.moveTo(0, height / 2); context.lineTo(width, height / 2);
  }
  context.stroke();
}

function drawSpectrum(bands) {
  const canvas = document.querySelector('#live-canvas');
  const context = canvas.getContext('2d');
  const width = Math.max(1, canvas.clientWidth), height = Math.max(1, canvas.clientHeight);
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.fillStyle = '#171619'; context.fillRect(0, 0, width, height);
  if (!bands?.length) return;
  const gap = 2, bar = Math.max(1, (width - gap * (bands.length - 1)) / bands.length);
  context.fillStyle = '#73d4c0';
  bands.forEach((level, index) => {
    const value = Math.max(0, Math.min(1, Number(level) || 0));
    const h = Math.max(1, value * height * .86);
    context.fillRect(index * (bar + gap), height - h, bar, h);
  });
}

function updateSideButtons() {
  document.querySelectorAll('[data-side]').forEach(button =>
    button.setAttribute('aria-pressed', String(button.dataset.side === side)));
}

function renderProjectOptions() {
  projectSelect.replaceChildren();
  if (!state.projects.length) {
    const option = document.createElement('option');
    option.value = ''; option.textContent = 'No album projects yet'; projectSelect.append(option);
    projectSelect.disabled = true;
    return;
  }
  projectSelect.disabled = false;
  for (const item of state.projects) {
    const option = document.createElement('option');
    option.value = item.id;
    option.textContent = item.label || 'Untitled album';
    projectSelect.append(option);
  }
}

function paintHistory() {
  const list = document.querySelector('#history');
  list.replaceChildren();
  const items = (state.history.items || []).filter(item => !project || item.project_id === project.id);
  for (const take of items) {
    const entry = document.createElement('li');
    const stateText = take.state === 'saved' ? 'Saved' : take.state === 'recording' ? 'Recording' :
      take.state === 'finalizing' ? 'Finalizing' : take.state === 'interrupted' ? 'Interrupted' : take.state;
    const time = take.published_at || take.created_at;
    const stamp = time ? new Date(time).toLocaleString() : 'Time unknown';
    const duration = take.frames ? `${(take.frames / 48000).toFixed(1)} sec` : 'Duration pending';
    const summary = document.createElement('span');
    summary.textContent = `Side ${take.side || '—'} · ${stateText} · ${duration} · ${stamp}`;
    entry.append(summary);
    if (take.state === 'saved' && take.locator) {
      const review = document.createElement('a');
      review.href = `/review/?side=${encodeURIComponent(take.locator)}`;
      review.textContent = `Review Side ${take.side || ''}`;
      entry.append(review);
    } else if (take.state === 'interrupted') {
      const attention = document.createElement('span');
      attention.textContent = 'Needs attention; the master was not confirmed saved.';
      entry.append(attention);
    }
    list.append(entry);
  }
  if (!items.length) {
    const entry = document.createElement('li');
    entry.textContent = 'No saved sides in this album yet.';
    list.append(entry);
  }
}

async function loadPin() {
  const summary = document.querySelector('#release-summary');
  if (!project) {
    say(summary, 'Create an album project to pin an edition.');
    say(document.querySelector('#album-artist'), 'Not set');
    return;
  }
  try {
    const pin = await request(`/api/release-pin?project_id=${encodeURIComponent(project.id)}`);
    pinRevision = pin.revision;
    const release = pin.release;
    say(summary, release
      ? `${release.title || 'Untitled release'} · ${release.artist || 'Unknown artist'} · ${release.date || 'Date unknown'} · ${release.media.map(m => m.format || 'Media').join(', ') || 'Format unknown'}`
      : 'No release pinned. You can record offline and add one later.');
    say(document.querySelector('#album-artist'), release?.artist || 'Not set');
    say(document.querySelector('#sleeve-title'), release?.title || project.label || 'Album cover');
  } catch (error) { say(summary, `Release information unavailable: ${error.message}`); }
}

async function refresh() {
  state = await request('/api/album');
  renderProjectOptions();
  const stored = localStorage.getItem('record-relay-project');
  project = state.projects.find(item => item.id === stored) || state.projects[0] || null;
  if (project) {
    localStorage.setItem('record-relay-project', project.id);
    projectSelect.value = project.id;
    say(document.querySelector('#album-heading'), project.label || 'Untitled album');
    label.value = project.label;
  } else {
    projectSelect.value = '';
    label.value = '';
    say(document.querySelector('#album-heading'), 'Choose an album');
  }
  if (project) {
    state = await request('/api/album?project_id=' + encodeURIComponent(project.id));
  }
  const saved = localStorage.getItem('record-relay-active-take');
  try { active = saved ? JSON.parse(saved) : null; }
  catch { localStorage.removeItem('record-relay-active-take'); active = null; }
  if (state.capture?.recording_id) {
    active = {request_id: state.capture.recording_id,
      side: state.capture.recording_side || active?.side || side};
    side = active.side;
    localStorage.setItem('record-relay-active-take', JSON.stringify(active));
  }
  updateSideButtons();
  paintHistory();
  await loadPin();
}

function updateCapture(value) {
  if (value.error) throw new Error(value.error);
  latestCapture = value;
  liveToggle.checked = value.live_view_client === true;
  document.querySelector('#motion-note').hidden = !reducedMotionQuery.matches;
  document.querySelector('#open-visualizer').disabled = reducedMotionQuery.matches || !liveToggle.checked;
  const liveCanvas = document.querySelector('#live-canvas');
  liveCanvas.hidden = !liveToggle.checked;
  if (liveToggle.checked) {
    const sequence = Number(value.visual_sequence) || 0;
    const age = value.visual_age_seconds;
    if (sequence > lastVisualSequence && age != null && age < 0.5) {
      latestSpectrum = receiveVisualization(value.visual_audio, document.querySelector('#visual-mode').value,
        document.querySelector('#visual-delay').value);
      const mode = document.querySelector('#visual-mode').value;
      document.querySelector('#live-canvas').setAttribute('aria-label', mode === 'spectrum'
        ? 'Live input frequency spectrum.' : 'Live input waveform.');
      if (latestSpectrum && Date.now() - lastSpectrumAnnouncement >= 3000) {
        const level = values => values.reduce((sum, item) => sum + item, 0) / Math.max(1, values.length);
        const describe = value => value < .08 ? 'very low' : value < .25 ? 'low' : value < .55 ? 'moderate' : 'high';
        const low = level(latestSpectrum.slice(0, 12));
        const mid = level(latestSpectrum.slice(12, 32));
        const high = level(latestSpectrum.slice(32));
        say(document.querySelector('#visual-summary'), `Approximate input spectrum: low frequencies ${describe(low)}, mid frequencies ${describe(mid)}, high frequencies ${describe(high)}.`);
        lastSpectrumAnnouncement = Date.now();
      }
      if (document.querySelector('#visual-mode').value === 'spectrum') drawSpectrum(latestSpectrum);
      else drawLiveView(value.visual_samples);
      lastVisualSequence = sequence;
    }
    const dropped = Number(value.visual_dropped_frames) || 0;
    const statusText = age == null || age >= 0.5
      ? 'Live view paused · waiting for a fresh input frame.'
      : (document.querySelector('#visual-mode').value === 'spectrum' ? 'Latest spectrum frame ' : 'Latest waveform frame ') + age.toFixed(1) + ' sec ago.';
    say(document.querySelector('#live-view-status'),
      statusText + ' Visuals do not prove listening. ' + dropped + ' stale frames dropped.');
  } else {
    say(document.querySelector('#live-view-status'), 'Live view is off. No visualization frames are retained.');
  }
  const queueToggle = document.querySelector('#offline-queue');
  queueToggle.checked = value.offline_queue === true;
  const queue = document.querySelector('#listen-queue');
  queue.replaceChildren();
  for (const item of value.listen_queue || []) {
    const row = document.createElement('li');
    const summary = document.createElement('span');
    summary.textContent = item.artist + ' · ' + item.title + ' · ' +
      (item.state === 'pending' ? 'Held: delivery outcome may be uncertain; no automatic retry'
        : 'Queued · expires ' + new Date(item.expires_at * 1000).toLocaleDateString());
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'secondary';
    if (item.state === 'queued') {
      remove.textContent = 'Delete';
      remove.setAttribute('aria-label', 'Delete queued listen ' + item.title + ' by ' + item.artist);
      remove.addEventListener('click', async () => {
      try {
        const result = await request('/control', {method: 'POST', body: {
          action: 'delete_queued_listen', play_id: item.play_id,
        }});
        if (result.error) throw new Error(result.error);
        await refreshCapture();
      } catch (error) { say(message, error.message); }
      });
      row.append(summary, remove);
    } else {
      row.append(summary);
    }
    queue.append(row);
  }
  if (!(value.listen_queue || []).length) {
    const empty = document.createElement('li');
    empty.textContent = 'No queued listens.';
    queue.append(empty);
  }
  if (value.recording_id) {
    active = {request_id: value.recording_id, side: value.recording_side || active?.side || side};
    side = active.side;
    localStorage.setItem('record-relay-active-take', JSON.stringify(active));
  } else if (value.recording === 'idle' && active) {
    localStorage.removeItem('record-relay-active-take');
    active = null;
  }
  const activeSide = value.recording_side || active?.side || side;
  const bounds = {oldest: value.buffered_oldest_frame, newest: value.buffered_newest_frame,
    generation: value.capture_generation};
  const boundary = state.side_a_boundary?.capture_generation === bounds.generation
    ? state.side_a_boundary.end_frame
    : (value.side_a_end_capture_generation === bounds.generation ? value.side_a_end_frame : null);
  const rangePanel = document.querySelector('#side-b-range');
  const range = document.querySelector('#side-b-start');
  rangePanel.hidden = side !== 'B' || !(Number.isInteger(bounds.oldest) &&
    Number.isInteger(bounds.newest) && bounds.newest > bounds.oldest);
  if (!rangePanel.hidden) {
    range.min = String(bounds.oldest);
    range.max = String(bounds.newest - 1);
    range.step = '4800';
    if (rangeGeneration !== bounds.generation) {
      range.value = String(boundary != null && boundary >= bounds.oldest && boundary < bounds.newest
        ? boundary : bounds.oldest);
      rangeGeneration = bounds.generation;
      document.querySelector('#overlap-confirm').checked = false;
    } else {
      range.value = String(Math.max(bounds.oldest, Math.min(bounds.newest - 1, Number(range.value))));
    }
    const selected = Number(range.value);
    const frameSeconds = Math.max(0, selected - bounds.oldest) / 48000;
    range.setAttribute('aria-valuetext', `${frameSeconds.toFixed(1)} seconds into retained audio`);
    say(document.querySelector('#side-b-range-summary'),
      (boundary == null ? 'No saved Side A boundary is known; start may include Side A audio. '
        : selected < boundary ? 'This start overlaps the saved Side A boundary. Confirm below. '
          : 'Starts at/after the saved Side A boundary. ') +
      'Selected offset: ' + frameSeconds.toFixed(1) + ' sec into the retained window.');
    document.querySelector('#overlap-confirm-label').hidden = !(boundary != null && selected < boundary);
    const unknown = document.querySelector('#unknown-boundary-confirm-label');
    unknown.hidden = boundary != null;
    if (boundary != null) document.querySelector('#unknown-boundary-confirm').checked = false;
  }
  const statusText = value.recording === 'recording'
    ? `Recording Side ${activeSide}`
    : value.recording === 'finalizing' ? `Finalizing Side ${activeSide}`
    : value.recording === 'failed' ? `Side ${activeSide} failed; not saved`
    : value.recording === 'finished' ? `Side ${activeSide} ready to save`
    : `Capture ${value.recording || 'ready'}`;
  say(captureStatus, statusText);
  previousCaptureStatus = statusText;
  const inputText = value.input_state === 'signal' ? 'Input signal present'
    : value.input_state === 'silence' ? 'Input active with silence'
    : value.input_state === 'stale' ? 'Input status is stale'
    : value.input_state === 'no_input' ? 'No input observed'
    : value.signal ? 'Input signal present' : 'Input status unavailable';
  const age = value.input_age_seconds == null ? '' : ` · ${value.input_age_seconds.toFixed(1)} sec ago`;
  const frames = value.recording_frames ? ` · ${value.recording_frames.toLocaleString()} frames${value.recording_limit_frames ? ` of ${value.recording_limit_frames.toLocaleString()}` : ''}` : '';
  const preRoll = value.recording_prefix_frames ? ` · ${value.recording_prefix_frames.toLocaleString()} pre-roll frames` : '';
  const clock = value.scrobble && value.listenbrainz_clock_trusted === false
    ? ' · ListenBrainz paused: system clock is untrusted' : '';
  const elapsed = value.recording_elapsed_seconds == null ? '' : ` · ${value.recording_elapsed_seconds.toFixed(1)} sec elapsed`;
  const detailText = `${inputText}${age} · ${Math.floor(value.buffered_seconds || 0)} sec in recent buffer${frames}${preRoll}${elapsed}${value.identify ? ' · Identify on' : ' · Identify off'}${value.scrobble ? ' · Scrobble on' : ' · Scrobble off'}${clock}`;
  say(detail, detailText);
  say(sideDetail, side === 'B'
    ? (value.late_save_uncertain
      ? 'Side B buffer will start at the oldest retained audio because the Side A boundary is unavailable. Review the start after saving; earlier audio has rolled out of the buffer.'
      : 'Side B buffer will start after the saved Side A boundary. Review the start after saving.')
    : 'Choose an album, then record a new side or save recent audio.');
  const busy = ['recording', 'finalizing'].includes(value.recording);
  recordButton.disabled = !project || busy;
  bufferButton.disabled = !project || busy || !(value.buffered_seconds > 0);
  stopButton.hidden = !['recording', 'finalizing', 'failed', 'finished'].includes(value.recording) || !active;
  document.querySelectorAll('[data-side]').forEach(button => { button.disabled = busy || Boolean(active); });
  document.querySelector('#now-playing').textContent = value.track
    ? `${value.track.title || 'Unknown title'} · ${value.track.artist || 'Unknown performer'}`
    : 'No recognized track.';
  if (value.recording_side) {
    side = value.recording_side;
    say(document.querySelector('#side-heading'), `Side ${side}${busy ? ' · recording' : ''}`);
    updateSideButtons();
  }
}

async function refreshCapture() {
  if (captureRefreshPending) return;
  captureRefreshPending = true;
  try {
    const capture = await request('/control', {method: 'POST', body: {
      action: 'status', project_id: project?.id || null,
      ...(document.querySelector('#live-view').checked && !document.hidden
        ? {visual_client_id: visualClientId} : {}),
    }});
    updateCapture(capture);
  } catch (error) {
    say(captureStatus, previousCaptureStatus ? `${previousCaptureStatus} · status unavailable` : 'Capture session unavailable');
    say(detail, error.message);
    recordButton.disabled = true;
    bufferButton.disabled = true;
    stopButton.hidden = true;
  } finally {
    captureRefreshPending = false;
  }
}

async function startTake(buffered) {
  if (!project) { say(message, 'Create or select an album first.'); return; }
  try {
    const body = {
      project_id: project.id, revision: project.revision, side, buffered, limit_seconds: 1800,
    };
    if (buffered && side === 'B') {
      body.buffered_start_frame = Number(document.querySelector('#side-b-start').value);
      body.overlap_ack = document.querySelector('#overlap-confirm').checked;
      body.unknown_boundary_ack = document.querySelector('#unknown-boundary-confirm').checked;
    }
    const value = await request('/api/takes/start', {method: 'POST', body});
    active = {request_id: value.request_id, side: value.side};
    localStorage.setItem('record-relay-active-take', JSON.stringify(active));
    say(message, `Recording Side ${side}. You can close this page and reconnect later.`);
    await refreshCapture();
  } catch (error) { say(message, error.message); }
}

recordButton.addEventListener('click', () => startTake(false));
bufferButton.addEventListener('click', () => startTake(true));
stopButton.addEventListener('click', async () => {
  if (!active) return;
  stopButton.disabled = true;
  try {
    const receipt = await request('/api/takes/stop', {method: 'POST', body: {request_id: active.request_id}});
    localStorage.removeItem('record-relay-active-take');
    active = null;
    say(message, `Side saved · ${receipt.frames.toLocaleString()} frames · ${receipt.sha256.slice(0, 12)}…`);
    await refresh();
    recordButton.focus();
  } catch (error) { say(message, error.message); }
  finally { stopButton.disabled = false; await refreshCapture(); }
});

document.querySelectorAll('[data-side]').forEach(button => button.addEventListener('click', () => {
  side = button.dataset.side;
  updateSideButtons();
  say(document.querySelector('#side-heading'), `Side ${side}`);
  refreshCapture();
}));
document.querySelector('#side-b-start').addEventListener('input', () => {
  if (latestCapture) updateCapture(latestCapture);
});

projectSelect.addEventListener('change', async event => {
  localStorage.setItem('record-relay-project', event.target.value);
  await refresh();
});

document.querySelector('#new-project').addEventListener('click', async () => {
  const value = prompt('Album name');
  if (value === null) return;
  try {
    project = await request('/api/projects', {method: 'POST', body: {label: value}});
    localStorage.setItem('record-relay-project', project.id);
    await refresh();
    say(message, 'New album project started.');
  } catch (error) { say(message, error.message); }
});

document.querySelector('#project-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!project) { say(message, 'Start a different album to create the first project.'); return; }
  try {
    project = await request(`/api/projects/${encodeURIComponent(project.id)}`, {method: 'POST', body: {revision: project.revision, label: label.value}});
    await refresh();
    say(message, 'Album name saved.');
  } catch (error) { say(message, error.message); }
});

document.querySelector('#release-search').addEventListener('submit', async event => {
  event.preventDefault();
  const target = document.querySelector('#release-candidates');
  const searchStatus = document.querySelector('#release-search-status');
  target.replaceChildren(Object.assign(document.createElement('legend'), {textContent: 'Matching editions (not pinned)'}));
  document.querySelector('#candidate-preview').replaceChildren();
  selectedRelease = null;
  document.querySelector('#pin-release').disabled = true;
  say(searchStatus, 'Searching MusicBrainz…');
  try {
    const result = await request('/api/releases/search', {method: 'POST', body: {
      album: document.querySelector('#search-album').value,
      artist: document.querySelector('#search-artist').value,
    }});
    target.replaceChildren(Object.assign(document.createElement('legend'), {textContent: 'Matching editions (not pinned)'}));
    if (!result.candidates.length) { say(searchStatus, 'No releases found. Try another album or continue without a pin.'); return; }
    say(searchStatus, `${result.candidates.length} candidate releases. Choose one to review, then pin it.`);
    for (const [index, candidate] of result.candidates.entries()) {
      const row = document.createElement('div'); row.className = 'candidate';
      const radio = document.createElement('input'); radio.type = 'radio'; radio.name = 'release-choice'; radio.value = candidate.release_mbid; radio.id = `release-${index}`;
      const text = document.createElement('label'); text.htmlFor = radio.id;
      const labels = candidate.labels.map(item => [item.name, item.catalog_number].filter(Boolean).join(' · ')).filter(Boolean).join(', ');
      text.textContent = `${candidate.title || 'Untitled'} · ${candidate.artist || 'Unknown artist'} · ${candidate.date || 'Date unknown'} · ${candidate.country || 'Country unknown'} · ${candidate.formats.join(', ') || 'Format unknown'} · ${labels || 'Label unknown'} · ${candidate.track_count} tracks`;
      radio.addEventListener('change', async () => {
        selectedRelease = candidate;
        document.querySelector('#pin-release').disabled = true;
        say(searchStatus, 'Loading edition details…');
        try {
          const preview = await request('/api/releases/preview', {method: 'POST', body: {release_mbid: candidate.release_mbid}});
          if (selectedRelease?.release_mbid !== candidate.release_mbid) return;
          selectedRelease = preview;
          const container = document.querySelector('#candidate-preview');
          container.replaceChildren();
          const heading = document.createElement('h4'); heading.textContent = `${preview.title || 'Untitled'} · ${preview.artist || 'Unknown artist'}`;
          const detail = document.createElement('p');
          const labelDetail = preview.labels.map(item => [item.name, item.catalog_number].filter(Boolean).join(' · ')).filter(Boolean).join(', ');
          detail.textContent = `${preview.date || 'Date unknown'} · ${preview.country || 'Country unknown'} · ${preview.media.map(item => item.format || 'Media').join(', ') || 'Format unknown'} · ${labelDetail || 'Label unknown'}`;
          const list = document.createElement('ol');
          let shown = 0;
          for (const medium of preview.media) for (const track of medium.tracks) {
            if (shown++ >= 60) break;
            const item = document.createElement('li');
            const performers = track.artists.length ? ` · ${track.artists.join(', ')}` : '';
            item.textContent = `${medium.position || 'Media'}-${track.number || track.position || ''} ${track.title || track.recording_title || 'Untitled'}${performers}`;
            list.append(item);
          }
          if (shown > 60) { const more = document.createElement('p'); more.textContent = 'Showing the first 60 tracks.'; container.append(more); }
          container.prepend(heading, detail);
          container.append(list);
          document.querySelector('#pin-release').disabled = false;
          say(searchStatus, 'Review the edition details, then choose Pin selected edition to apply it.');
        } catch (error) { say(searchStatus, `Edition details unavailable: ${error.message}`); }
      });
      row.append(radio, text); target.append(row);
    }
  } catch (error) { say(searchStatus, `Release search unavailable: ${error.message}`); }
});

document.querySelector('#pin-release').addEventListener('click', async () => {
  if (!selectedRelease || !project) return;
  if (!confirm(`Pin ${selectedRelease.title} by ${selectedRelease.artist} to this album?`)) return;
  try {
    const pin = await request('/api/releases/pin', {method: 'POST', body: {project_id: project.id, release_mbid: selectedRelease.release_mbid, revision: pinRevision}});
    pinRevision = pin.revision;
    say(message, 'Release pinned to this album project.');
    await loadPin();
  } catch (error) { say(message, error.message); }
});

for (const [id, action] of [['identify', 'identify'], ['scrobble', 'scrobble']]) {
  document.querySelector(`#${id}`).addEventListener('change', async event => {
    try {
      const value = await request('/control', {method: 'POST', body: {action, enabled: event.target.checked}});
      if (value.error) throw new Error(value.error);
      say(message, `${id === 'identify' ? 'Identify' : 'Scrobble'} ${event.target.checked ? 'enabled' : 'disabled'}.`);
    } catch (error) { event.target.checked = false; say(message, error.message); }
  });
}

document.querySelector('#offline-queue').addEventListener('change', async event => {
  try {
    const value = await request('/control', {method: 'POST', body: {
      action: 'offline_queue', enabled: event.target.checked,
    }});
    if (value.error) throw new Error(value.error);
    updateCapture(value);
    say(message, 'Offline listen queue ' + (event.target.checked ? 'enabled.' : 'disabled.'));
  } catch (error) {
    event.target.checked = false;
    say(message, error.message);
  }
});

document.querySelector('#live-view').addEventListener('change', async event => {
  try {
    const value = await request('/control', {method: 'POST', body: {
      action: 'visual_client', enabled: event.target.checked,
      visual_client_id: visualClientId,
    }});
    if (value.error) throw new Error(value.error);
    updateCapture(value);
    document.querySelector('#open-visualizer').disabled = reducedMotionQuery.matches || !event.target.checked;
    if (!event.target.checked) {
      latestSpectrum = null; clearVisualization();
      document.querySelector('#visualizer-dialog').close();
    }
    startPolling();
  } catch (error) {
    event.target.checked = false;
    say(message, error.message);
  }
});

document.querySelector('#visual-mode').addEventListener('change', () => {
  const mode = document.querySelector('#visual-mode').value;
  document.querySelector('#live-canvas').setAttribute('aria-label', mode === 'spectrum'
    ? 'Live input frequency spectrum.' : 'Live input waveform.');
  if (!latestCapture) return;
  if (mode === 'spectrum') drawSpectrum(latestSpectrum);
  else drawLiveView(latestCapture.visual_samples);
});
document.querySelector('#visual-delay').addEventListener('input', event => {
  const delay = Number(event.target.value);
  document.querySelector('#visual-delay-value').value = delay ? `${delay} ms` : '0 ms';
});

function startPolling() {
  if (pollTimer !== null) clearInterval(pollTimer);
  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    refreshCapture();
    if (Date.now() - lastAlbumRefresh >= 2000) {
      lastAlbumRefresh = Date.now();
      request('/api/album').then(data => { state = data; paintHistory(); }).catch(() => {});
    }
  }, document.querySelector('#live-view').checked ? (motionQuery.matches ? 1000 : 100) : 2000);
}

updateSideButtons();
refresh().then(refreshCapture).catch(error => { say(message, `Local catalog unavailable: ${error.message}`); });
startPolling();
document.addEventListener('visibilitychange', () => {
  if (document.hidden && document.querySelector('#live-view').checked) {
    request('/control', {method: 'POST', body: {
      action: 'visual_client', enabled: false, visual_client_id: visualClientId,
    }}).catch(() => {});
  } else if (!document.hidden) {
    refreshCapture();
    startPolling();
  }
});
reducedMotionQuery.addEventListener('change', event => {
  applyMotionPreference(event.matches);
  if (event.matches) say(message, 'Reduced motion: the spectrum is static and refreshes once per second; animation is disabled.');
  startPolling();
});
