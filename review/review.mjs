import {splitPlan, validateWaveform} from "./splits.mjs";

const el = id => document.getElementById(id);
const player = el("player");
const canvas = el("peaks");
const context = canvas.getContext("2d");
let waveform;
let recordingUrl;
let master;
let points = [];
let editing;
let generation = 0;
let savedSide;
let selectedFrame;
let restoredCover;
let reviewDirty = false;
function dirty() {
  if (!savedSide) return;
  reviewDirty = true;
  el("draft-status").textContent = "Unsaved changes";
}
window.addEventListener("beforeunload", event => {
  if (reviewDirty) event.preventDefault();
});

const confirmed = new Set();
const trackTags = new Map();
const fingerprintCache = new Map();
const fingerprintInFlight = new Map();

function resetFingerprintConsent() {
  const consent = el("acoustid-consent");
  if (consent.checked) {
    consent.checked = false;
    consent.dispatchEvent(new Event("change"));
  }
}

function setSelectedFrame(frame) {
  if (frame !== selectedFrame) resetFingerprintConsent();
  selectedFrame = frame;
}

async function sideRequest(action, payload) {
  const response = await fetch("/recordings/" + action, {
    method: "POST",
    headers: {"Content-Type": "application/json",
      "X-Control-Token": document.querySelector("main").dataset.reviewToken},
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error || "Could not process the saved side.");
  }
  return response;
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function coverData() {
  const file = el("cover").files[0];
  if (!file) return restoredCover;
  if (file.size > 6000000 || !["image/png", "image/jpeg"].includes(file.type)) {
    throw new Error("Choose a PNG or JPEG cover up to 6 MB.");
  }
  const image = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Could not open the cover image."));
    reader.readAsDataURL(file);
  });
  return {format: file.type.split("/")[1], data: image.split(",")[1]};
}

function trackExportOptions(frame, index) {
  const tags = {...trackTags.get(frame)};
  tags.artist ||= el("album-artist").value;
  tags.album ||= el("album-title").value;
  tags.album_artist = el("album-artist").value;
  return {...tags, start: frame, end: points[index + 1] ?? waveform.frames, track: String(index + 1)};
}

async function exportTrack(frame, index) {
  const tags = trackExportOptions(frame, index);
  const format = el("format").value;
  const response = await sideRequest("export", {...tags, name: savedSide,
    format, cover: format === "wav" ? undefined : await coverData()});
  const safe = [tags.artist, tags.title].filter(Boolean).join(" - ")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "").trim().slice(0, 110);
  const name = String(index + 1).padStart(2, "0") + " - " +
    (safe || savedSide.replace(/\.wav$/i, "")) + "." + format;
  saveBlob(await response.blob(), name);
}

async function loadSavedSide(name) {
  const current = ++generation;
  el("manual-sources").hidden = true;
  el("load-progress").hidden = false;
  el("retry-side").hidden = true;
  el("status").textContent = "Loading the waveform and suggested track starts…";
  try {
    if (recordingUrl) URL.revokeObjectURL(recordingUrl);
    master = savedSide = name;
    el("side-heading").textContent = name.replace(/\.wav$/i, "");
    recordingUrl = undefined;
    player.src = "/recordings/audio?side=" + encodeURIComponent(name);
    const graph = validateWaveform(await (await sideRequest("waveform", {name})).json());
    if (generation !== current) return;
    waveform = graph;
    const identity = graph.matches?.[0]?.tags || {};
    el("album-artist").value = identity.artist || "";
    el("album-title").value = identity.album || "";
    el("load-progress").hidden = true;
    points = [...(graph.suggested_starts || [])];
    try {
      const draft = await (await sideRequest("draft", {name})).json();
      if (generation === current && draft.frames === graph.frames &&
          draft.sample_rate === graph.sample_rate && Array.isArray(draft.points)) {
        points = splitPlan(name, graph, draft.points).tracks.map(track => track.start);
        for (const [frame, tags] of Array.isArray(draft.tags) ? draft.tags : []) {
          if (points.includes(frame) && tags && typeof tags === "object") trackTags.set(frame, tags);
        }
        for (const frame of Array.isArray(draft.confirmed) ? draft.confirmed : []) {
          if (points.includes(frame)) confirmed.add(frame);
        }
        el("album-artist").value = typeof draft.artist === "string" ? draft.artist : "";
        el("album-title").value = typeof draft.album === "string" ? draft.album : "";
        if (["flac","mp3","wav"].includes(draft.format)) el("format").value = draft.format;
        if (["png","jpeg"].includes(draft.cover?.format) && typeof draft.cover.data === "string" &&
            /^[A-Za-z0-9+/=]+$/.test(draft.cover.data)) {
          restoredCover = draft.cover;
          el("cover-preview").src = "data:image/" + restoredCover.format + ";base64," + restoredCover.data;
          el("cover-preview").hidden = false;
        }
        setSelectedFrame(points.includes(draft.selected) ? draft.selected : points[0]);
        el("draft-status").textContent = "Saved review restored.";
      }
    } catch {
      el("draft-status").textContent = "Could not load a saved review. The master is available.";
    }
    render();
  } catch (error) {
    if (generation === current) {
      el("status").textContent = error.message;
      el("load-progress").hidden = true;
      el("retry-side").hidden = false;
    }
  }
}

function ready() {
  return Boolean(master && waveform && Number.isFinite(player.duration) &&
    Math.abs(player.duration - waveform.frames / waveform.sample_rate) <= .015);
}

function render() {
  const usable = ready();
  el("export-options").hidden = !savedSide;

  el("position").disabled = el("add").disabled = !usable;
  el("suggest").disabled = !usable || !waveform?.suggested_starts?.length;
  el("save").disabled = !usable || points.length === 0;
  if (master && waveform && !usable && Number.isFinite(player.duration)) {
    el("status").textContent = "The recording length differs from the waveform. Check the files.";
  } else if (usable) {
    el("status").textContent = savedSide ? "Choose a track to review its start and tags." :
      "Ready to review. Confirm the waveform was generated from this side.";
  }
  const {width, height} = canvas;
  context.clearRect(0, 0, width, height);
  if (waveform) {
    context.fillStyle = "#87d9cb";
    const column = width / waveform.peaks.length;
    waveform.peaks.forEach((pair, index) => pair.forEach((peak, channel) => {
      const amplitude = Math.max(1, peak / 32768 * height / 4);
      context.fillRect(index * column, (channel + .5) * height / 2 - amplitude, column, amplitude * 2);
    }));
    context.fillStyle = "#f3bd81";
    points.forEach(frame => context.fillRect(frame / waveform.frames * width, 0, 2, height));
  }
  if (!points.includes(selectedFrame)) setSelectedFrame(points[0]);
  el("splits").parentElement.append(el("track-editor"));
  el("splits").replaceChildren(...points.map((frame, index) => {
    const item = document.createElement("li");
    item.dataset.frame = frame;
    const seconds = frame / waveform.sample_rate;
    if (!trackTags.has(frame)) {
      const end = points[index + 1] ?? waveform.frames;
      const match = (waveform.matches || []).find(cue => cue.frame >= frame && cue.frame < end);
      trackTags.set(frame, match ? {...match.tags, recognized: true} : {});
    }
    const tags = trackTags.get(frame);
    const heading = document.createElement("button");
    heading.className = "track-select secondary";
    heading.textContent = (tags.title || "Track " + (index + 1)) + " · " +
      Math.floor(seconds / 60) + ":" + (seconds % 60).toFixed(1).padStart(4, "0");
    heading.setAttribute("aria-pressed", String(frame === selectedFrame));
    heading.setAttribute("aria-label", "Seek track start at " + seconds.toFixed(3) + " seconds: " + (tags.title || "Track " + (index + 1)));
    heading.onclick = () => {
      setSelectedFrame(frame);
      player.currentTime = seconds;
      render();
      el("splits").querySelector('[aria-pressed="true"]')?.focus({preventScroll:true});
    };
    item.append(heading);
    if (savedSide) {
      const state = document.createElement("small");
      state.textContent = confirmed.has(frame) ? "Confirmed" : tags.recognized ? "Identified" : "Unknown";
      item.append(state);
    } else {
      for (const [label, action] of [
        ["Edit", () => { editing = frame; el("seconds").value = seconds; el("seconds").focus(); }],
        ["Remove", () => { points = points.filter(point => point !== frame); render(); }],
      ]) {
        const button = document.createElement("button");
        button.className = "secondary";
        button.textContent = label;
        button.setAttribute("aria-label", label + " track start at " + seconds.toFixed(3) + " seconds");
        button.onclick = action;
        item.append(button);
      }
    }
    return item;
  }));
  const workspace = el("track-editor").closest(".track-workspace") || el("splits").parentElement;
  if (savedSide && matchMedia("(max-width: 540px)").matches && selectedFrame !== undefined) {
    el("splits").querySelector(`li[data-frame="${selectedFrame}"]`)?.append(el("track-editor"));
  } else {
    workspace.append(el("track-editor"));
  }
  renderEditor();
}

function renderEditor() {
  const frame = selectedFrame;
  el("track-editor").hidden = !savedSide || !ready() || frame === undefined;
  if (el("track-editor").hidden) {
    el("long-track-warning").hidden = true;
    el("recover-splits").hidden = true;
    el("fingerprint-sample-wrap").hidden = true;
    return;
  }
  const index = points.indexOf(frame);
  const tags = trackTags.get(frame);
  el("editor-heading").textContent = "Track " + (index + 1);
  const trackEnd = points[index + 1] ?? waveform.frames;
  const trackSeconds = (trackEnd - frame) / waveform.sample_rate;
  const unusuallyLong = trackSeconds >= 12 * 60;
  el("long-track-warning").hidden = !unusuallyLong;
  if (unusuallyLong) {
    el("long-track-warning").textContent = `This segment is ${Math.floor(trackSeconds / 60)} minutes long. It may contain missed track starts. The original WAV is unchanged; listen through the segment and check waveform suggestions before exporting.`;
  }
  const recoveryStarts = (waveform.suggested_starts || []).filter(point =>
    point >= frame + waveform.sample_rate * 2 && point < trackEnd - waveform.sample_rate * 2 &&
    !points.includes(point)).slice(0, 20);
  const recovery = el("recover-splits");
  recovery.hidden = !recoveryStarts.length;
  recovery.textContent = `Add ${recoveryStarts.length}${recoveryStarts.length === 20 ? "+" : ""} suggested start${recoveryStarts.length === 1 ? "" : "s"} within this track`;
  recovery.onclick = () => {
    points = [...new Set([...points, ...recoveryStarts])].sort((a, b) => a - b);
    dirty();
    render();
    el("editor-status").textContent = "Waveform-based starts were added as unconfirmed markers. Listen around each one and adjust or remove it before exporting.";
  };
  el("fingerprint-sample-wrap").hidden = trackSeconds <= 120;
  if (trackSeconds <= 120) el("fingerprint-sample").value = "beginning";
  for (const key of ["title", "artist", "album"]) {
    const field = el("track-" + key);
    field.value = tags[key] || "";
    field.oninput = () => {
      tags[key] = field.value;
      confirmed.delete(frame);
      dirty();
    };
  }
  el("editor-boundary").value = frame / waveform.sample_rate;
  el("move-marker").onclick = () => {
    editing = frame;
    el("seconds").value = el("editor-boundary").value;
    el("add").click();
  };
  for (const [id, step] of [["earlier-marker",-0.1],["later-marker",0.1]]) {
    el(id).onclick = () => {
      el("editor-boundary").value = Math.max(0, frame / waveform.sample_rate + step).toFixed(3);
      el("move-marker").click();
    };
  }
  el("preview-marker").onclick = () => { player.currentTime = Math.max(0, frame / waveform.sample_rate - 2); };
  el("remove-marker").onclick = () => {
    points = points.filter(point => point !== frame);
    trackTags.delete(frame);
    confirmed.delete(frame);
    dirty();
    if (editing === frame) editing = undefined;
    setSelectedFrame(points[Math.min(index, points.length - 1)]);
    render();
  };
  el("confirm-track").onclick = () => {
    if (!tags.title?.trim() || !(tags.artist?.trim() || el("album-artist").value.trim())) {
      el("editor-status").textContent = "Enter a title and artist to confirm this track.";
      return;
    }
    confirmed.add(frame);
    dirty();
    render();
    el("editor-status").textContent = "Track tags confirmed.";
  };
  el("export-track").onclick = async () => {
    el("export-track").disabled = true;
    try {
      await exportTrack(frame, index);
      el("editor-status").textContent = "Track exported.";
    } catch (error) {
      el("editor-status").textContent = error.message;
    } finally {
      el("export-track").disabled = false;
    }
  };
  el("fingerprint-track").onclick = () => lookupFingerprint(frame, index, false);
  el("fingerprint-recheck").onclick = () => lookupFingerprint(frame, index, true);
}

function fingerprintKey(frame, end, sample) {
  return `${savedSide}\0${waveform.frames}\0${waveform.sample_rate}\0${frame}\0${end}\0${sample}`;
}

function showFingerprintResult(result, frame, key, cached = false) {
  if (selectedFrame !== frame) return;
  const target = el("fingerprint-suggestions");
  target.replaceChildren();
  const candidates = result.matches.flatMap(match =>
    match.recordings.map(recording => ({...recording, score: match.score})));
  const sampleDuration = Number.isInteger(result.sample_duration_seconds)
    ? result.sample_duration_seconds : Math.min(120, result.duration_seconds || 0);
  const samplePosition = result.sample_position || "beginning";
  const sampled = `${cached ? "Cached result; no new request sent. " : ""}Checked ${sampleDuration}s from the ${samplePosition} of this segment; full segment duration sent: ${result.duration_seconds}s.`;
  el("fingerprint-result").textContent = candidates.length
    ? `AcoustID returned ${candidates.length} possible match${candidates.length === 1 ? "" : "es"}. ${sampled} Suggestions do not change tags.`
    : `No AcoustID match was found. ${sampled} Nothing was changed.`;
  for (const candidate of candidates) {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = candidate.title + (candidate.artist ? " · " + candidate.artist : "") +
      " · " + Math.round(candidate.score * 100) + "%";
    row.append(label);
    if (candidate.artist) {
      const use = document.createElement("button");
      use.type = "button";
      use.className = "secondary";
      use.textContent = "Fill blank title and artist";
      use.onclick = () => {
        const tags = trackTags.get(frame) || {};
        const titleField = el("track-title"), artistField = el("track-artist");
        const fillTitle = !String(tags.title || "").trim();
        const fillArtist = !String(tags.artist || "").trim();
        if (fillTitle) { tags.title = candidate.title; titleField.value = candidate.title; }
        if (fillArtist) { tags.artist = candidate.artist; artistField.value = candidate.artist; }
        trackTags.set(frame, tags);
        if (fillTitle || fillArtist) { confirmed.delete(frame); dirty(); }
        el("editor-status").textContent = fillTitle || fillArtist
          ? "Suggestions filled only into blank fields. Existing manual values were kept; review and confirm tags yourself."
          : "Both fields already contain values, so your manual tags were kept unchanged.";
      };
      row.append(use);
    }
    target.append(row);
  }
  el("fingerprint-recheck").hidden = !fingerprintCache.has(key);
}

async function lookupFingerprint(frame, index, force) {
  const button = el("fingerprint-track");
  const recheck = el("fingerprint-recheck");
  const interval = trackExportOptions(frame, index);
  const sample = el("fingerprint-sample").value;
  const key = fingerprintKey(frame, points[index + 1] ?? waveform.frames, sample);
  if (!force && fingerprintCache.has(key)) {
    showFingerprintResult(fingerprintCache.get(key), frame, key, true);
    return;
  }
  if (fingerprintInFlight.has(key)) return fingerprintInFlight.get(key);
  button.disabled = recheck.disabled = true;
  const range = (interval.end - interval.start) / waveform.sample_rate;
  const excerptText = range > 120 ? `the ${sample} 120-second excerpt of this ${Math.floor(range / 60)}-minute segment` : "the reviewed segment";
  el("fingerprint-result").textContent = `Creating a local fingerprint for ${excerptText} and querying AcoustID… The WAV is not uploaded.`;
  el("fingerprint-suggestions").replaceChildren();
  const operation = (async () => {
    try {
      const response = await sideRequest("fingerprint", {
        name: savedSide, start: interval.start, end: interval.end,
        sample_position: sample, noncommercial_ack: el("acoustid-consent").checked,
      });
      const result = await response.json();
      fingerprintCache.set(key, result);
      showFingerprintResult(result, frame, key);
    } catch (error) {
      if (selectedFrame === frame) {
        el("fingerprint-result").textContent = `AcoustID lookup failed: ${error.message}. Your audio and tags were not changed.`;
        el("fingerprint-suggestions").replaceChildren();
      }
    } finally {
      fingerprintInFlight.delete(key);
      button.disabled = !el("acoustid-consent").checked;
      recheck.disabled = false;
    }
  })();
  fingerprintInFlight.set(key, operation);
  return operation;
}

function reset() {
  points = [];
  trackTags.clear();
  confirmed.clear();
  setSelectedFrame(undefined);
  editing = undefined;
  el("seconds").value = "0";
  el("status").textContent = "Waiting for a matching recording and waveform.";
}

el("recording").onchange = event => {
  generation++;
  savedSide = undefined;
  if (recordingUrl) URL.revokeObjectURL(recordingUrl);
  player.removeAttribute("src");
  master = event.target.files[0]?.name;
  recordingUrl = master ? URL.createObjectURL(event.target.files[0]) : undefined;
  if (recordingUrl) player.src = recordingUrl;
  else player.load();
  reset();
  render();
};
player.onloadedmetadata = render;
player.onerror = () => { el("status").textContent = "The side master could not be opened."; };
el("waveform").onchange = async event => {
  const request = ++generation;
  waveform = undefined;
  reset();
  render();
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 250000) throw new Error("Waveform JSON exceeds the supported size.");
    const data = validateWaveform(JSON.parse(await file.text()));
    if (request !== generation) return;
    waveform = data;
  } catch (error) {
    if (request !== generation) return;
    el("status").textContent = error.message;
  }
  render();
};
el("suggest").onclick = () => {
  if (!ready()) return;
  points = [...new Set([...points, ...(waveform.suggested_starts || [])])].sort((a, b) => a - b);
  editing = undefined;
  render();
  dirty();
  el("status").textContent = "Suggested starts added. Listen around each one before exporting.";
};
canvas.onkeydown = event => {
  if (!ready() || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const target = event.key === "Home" ? 0 : event.key === "End" ? player.duration :
    player.currentTime + (event.key === "ArrowRight" ? 5 : -5);
  player.currentTime = Math.max(0, Math.min(player.duration, target));
};
player.ontimeupdate = () => {
  if (!ready()) return;
  const frame = player.currentTime * waveform.sample_rate;
  const selected = points.findLast(point => point <= frame);
  for (const item of el("splits").children) {
    if (Number(item.dataset.frame) === selected) item.setAttribute("aria-current", "true");
    else item.removeAttribute("aria-current");
  }
};
let dragging;
function fractionAt(event) {
  const bounds = canvas.getBoundingClientRect();
  return Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
}
canvas.addEventListener("pointerdown", event => {
  if (!ready() || event.button !== 0) return;
  const bounds = canvas.getBoundingClientRect();
  const nearby = points.find(frame => Math.abs(
    frame / waveform.frames - fractionAt(event)) * bounds.width < 10);
  if (nearby === undefined) return;
  dragging = {frame: nearby, x: event.clientX, pointer: event.pointerId};
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener("pointercancel", () => { dragging = undefined; });
canvas.addEventListener("pointerup", event => {
  if (!ready()) return;
  const place = fractionAt(event);
  const marker = dragging;
  dragging = undefined;
  if (marker?.pointer === event.pointerId && Math.abs(event.clientX - marker.x) >= 4) {
    editing = marker.frame;
    el("seconds").value = Math.min((waveform.frames - 1) / waveform.sample_rate,
      place * player.duration);
    el("add").click();
  } else {
    player.currentTime = player.duration * place;
  }
});
el("position").onclick = () => { el("seconds").value = player.currentTime; };
el("add").onclick = () => {
  if (!ready()) return;
  const frame = Math.round(el("seconds").valueAsNumber * waveform.sample_rate);
  if (!Number.isSafeInteger(frame) || frame < 0 || frame >= waveform.frames) {
    el("status").textContent = "Enter a time within the side master.";
    return;
  }
  if (editing !== undefined && editing !== frame && trackTags.has(editing)) {
    trackTags.set(frame, trackTags.get(editing));
    trackTags.delete(editing);
    confirmed.delete(editing);
  }
  points = [...new Set([...points.filter(point => point !== editing), frame])].sort((a, b) => a - b);
  setSelectedFrame(frame);
  dirty();
  editing = undefined;
  render();
};
el("save").onclick = () => {
  if (!ready()) return;
  const json = JSON.stringify(splitPlan(master, waveform, points), null, 2);
  const url = URL.createObjectURL(new Blob([json], {type: "application/json"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = "split-plan.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

el("export-side").onclick = async () => {
  if (!savedSide || !ready() || !points.length) return;
  const button = el("export-side");
  button.disabled = true;
  el("draft-status").textContent = "Exporting tracks. This may take a minute on the Pi.";
  try {
    const format = el("format").value;
    const response = await sideRequest("export-side", {name: savedSide, format,
      tracks: points.map(trackExportOptions), cover: format === "wav" ? undefined : await coverData()});
    saveBlob(await response.blob(), savedSide.replace(/\.wav$/i, "") + "-tracks.zip");
    el("draft-status").textContent = "Track archive downloaded.";
  } catch (error) {
    el("draft-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
};
el("save-draft").onclick = async () => {
  if (!savedSide || !ready()) return;
  const button = el("save-draft");
  button.disabled = true;
  try {
    await sideRequest("draft", {name: savedSide, draft: {
      frames: waveform.frames, sample_rate: waveform.sample_rate, points,
      tags: [...trackTags.entries()], confirmed: [...confirmed], selected: selectedFrame,
      artist: el("album-artist").value, album: el("album-title").value,
      format: el("format").value, cover: await coverData(),
    }});
    reviewDirty = false;
    el("draft-status").textContent = "Review saved on the Pi.";
  } catch (error) {
    el("draft-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
};

let artworkUrl;
el("cover").onchange = () => {
  dirty();
  if (artworkUrl) URL.revokeObjectURL(artworkUrl);
  const file = el("cover").files[0];
  artworkUrl = file && ["image/png","image/jpeg"].includes(file.type) ?
    URL.createObjectURL(file) : undefined;
  el("cover-preview").hidden = !artworkUrl;
  if (artworkUrl) el("cover-preview").src = artworkUrl;
  else el("cover-preview").removeAttribute("src");
};
el("acoustid-consent").addEventListener("change", event => {
  el("fingerprint-track").disabled = !event.target.checked;
  if (!event.target.checked) {
    el("fingerprint-result").textContent = "";
    el("fingerprint-suggestions").replaceChildren();
    el("fingerprint-recheck").hidden = true;
  }
});
el("fingerprint-sample").addEventListener("change", () => {
  el("fingerprint-result").textContent = "Excerpt changed. Choose Identify selected track to check it.";
  el("fingerprint-suggestions").replaceChildren();
  el("fingerprint-recheck").hidden = true;
});
el("remove-cover").onclick = () => {
  restoredCover = undefined;
  el("cover").value = "";
  el("cover").onchange();
};
el("album-artist").oninput = el("album-title").oninput = dirty;
el("format").onchange = () => { dirty(); el("cover").disabled = el("format").value === "wav"; };
const side = new URLSearchParams(location.search).get("side");
if (side) { el("retry-side").onclick = () => loadSavedSide(side); loadSavedSide(side); }
