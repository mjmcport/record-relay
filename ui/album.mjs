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
let state = {projects: [], history: {items: []}, capture: {recording: 'unavailable'}};
let project = null;
let side = 'A';
let active = null;
let pinRevision = 0;
let selectedRelease = null;
let previousCaptureStatus = '';

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
  if (value.recording_id) {
    active = {request_id: value.recording_id, side: value.recording_side || active?.side || side};
    side = active.side;
    localStorage.setItem('record-relay-active-take', JSON.stringify(active));
  } else if (value.recording === 'idle' && active) {
    localStorage.removeItem('record-relay-active-take');
    active = null;
  }
  const activeSide = value.recording_side || active?.side || side;
  const statusText = value.recording === 'recording'
    ? `Recording Side ${activeSide} · ${(value.recording_elapsed_seconds || 0).toFixed(1)} sec`
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
  const detailText = `${inputText}${age} · ${Math.floor(value.buffered_seconds || 0)} sec in recent buffer${frames}${preRoll}${value.identify ? ' · Identify on' : ' · Identify off'}${value.scrobble ? ' · Scrobble on' : ' · Scrobble off'}`;
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
  try {
    updateCapture(await request('/control', {method: 'POST', body: {action: 'status'}}));
  } catch (error) {
    say(captureStatus, previousCaptureStatus ? `${previousCaptureStatus} · status unavailable` : 'Capture session unavailable');
    say(detail, error.message);
    recordButton.disabled = true;
    bufferButton.disabled = true;
    stopButton.hidden = true;
  }
}

async function startTake(buffered) {
  if (!project) { say(message, 'Create or select an album first.'); return; }
  try {
    const value = await request('/api/takes/start', {method: 'POST', body: {
      project_id: project.id, revision: project.revision, side, buffered, limit_seconds: 1800,
    }});
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

updateSideButtons();
refresh().then(refreshCapture).catch(error => { say(message, `Local catalog unavailable: ${error.message}`); });
setInterval(() => {
  if (document.hidden) return;
  refreshCapture();
  request('/api/album').then(data => { state = data; paintHistory(); }).catch(() => {});
}, 2000);
