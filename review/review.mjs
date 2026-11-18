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
const confirmed = new Set();
const trackTags = new Map();

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
  if (!file) return undefined;
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

async function exportTrack(frame, index) {
  const tags = {...trackTags.get(frame)};
  tags.artist ||= el("album-artist").value;
  tags.album ||= el("album-title").value;
  tags.album_artist = el("album-artist").value;
  const format = el("format").value;
  const response = await sideRequest("export", {...tags, name: savedSide,
    start: frame, end: points[index + 1] ?? waveform.frames, track: String(index + 1),
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
  if (!points.includes(selectedFrame)) selectedFrame = points[0];
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
      selectedFrame = frame;
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
  renderEditor();
}

function renderEditor() {
  const frame = selectedFrame;
  el("track-editor").hidden = !savedSide || !ready() || frame === undefined;
  if (el("track-editor").hidden) return;
  const index = points.indexOf(frame);
  const tags = trackTags.get(frame);
  el("editor-heading").textContent = "Track " + (index + 1);
  for (const key of ["title", "artist", "album"]) {
    const field = el("track-" + key);
    field.value = tags[key] || "";
    field.oninput = () => {
      tags[key] = field.value;
      confirmed.delete(frame);
    };
  }
  el("editor-boundary").value = frame / waveform.sample_rate;
  el("move-marker").onclick = () => {
    editing = frame;
    el("seconds").value = el("editor-boundary").value;
    el("add").click();
  };
  el("remove-marker").onclick = () => {
    points = points.filter(point => point !== frame);
    trackTags.delete(frame);
    confirmed.delete(frame);
    if (editing === frame) editing = undefined;
    selectedFrame = points[Math.min(index, points.length - 1)];
    render();
  };
  el("confirm-track").onclick = () => {
    if (!tags.title?.trim() || !(tags.artist?.trim() || el("album-artist").value.trim())) {
      el("editor-status").textContent = "Enter a title and artist to confirm this track.";
      return;
    }
    confirmed.add(frame);
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
}

function reset() {
  points = [];
  trackTags.clear();
  confirmed.clear();
  selectedFrame = undefined;
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
canvas.onclick = event => {
  if (!ready()) return;
  const bounds = canvas.getBoundingClientRect();
  player.currentTime = player.duration * Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
};
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
  selectedFrame = frame;
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

let artworkUrl;
el("cover").onchange = () => {
  if (artworkUrl) URL.revokeObjectURL(artworkUrl);
  const file = el("cover").files[0];
  artworkUrl = file && ["image/png","image/jpeg"].includes(file.type) ?
    URL.createObjectURL(file) : undefined;
  el("cover-preview").hidden = !artworkUrl;
  if (artworkUrl) el("cover-preview").src = artworkUrl;
  else el("cover-preview").removeAttribute("src");
};
el("format").onchange = () => { el("cover").disabled = el("format").value === "wav"; };
const side = new URLSearchParams(location.search).get("side");
if (side) { el("retry-side").onclick = () => loadSavedSide(side); loadSavedSide(side); }
