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
  const tags = trackTags.get(frame) || {};
  const response = await sideRequest("export", {...tags, name: savedSide,
    start: frame, end: points[index + 1] ?? waveform.frames, track: String(index + 1),
    cover: await coverData()});
  saveBlob(await response.blob(), savedSide.replace(/\.wav$/i, "") + "-" + (index + 1) + ".flac");
}

async function loadSavedSide(name) {
  const current = ++generation;
  el("status").textContent = "Opening the saved side and finding possible track starts…";
  try {
    const response = await sideRequest("preview", {name});
    const audio = await response.blob();
    const graph = validateWaveform(await (await sideRequest("waveform", {name})).json());
    if (generation !== current) return;
    if (recordingUrl) URL.revokeObjectURL(recordingUrl);
    master = savedSide = name;
    waveform = graph;
    recordingUrl = URL.createObjectURL(audio);
    player.src = recordingUrl;
    render();
  } catch (error) {
    if (generation === current) el("status").textContent = error.message;
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
    el("status").textContent = "Ready to review. Confirm the waveform was generated from this side.";
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
  el("splits").replaceChildren(...points.map((frame, index) => {
    const item = document.createElement("li");
    const seconds = frame / waveform.sample_rate;
    item.append(document.createTextNode(seconds.toFixed(3) + " s "));
    for (const [label, action] of [
      ["Seek", () => { player.currentTime = seconds; }],
      ["Edit", () => { editing = frame; el("seconds").value = seconds; el("seconds").focus(); }],
      ["Remove", () => { points = points.filter(point => point !== frame); if (editing === frame) editing = undefined; render(); }],
    ]) {
      const button = document.createElement("button");
      button.textContent = label;
      button.setAttribute("aria-label", label + " track start at " + seconds.toFixed(3) + " seconds");
      button.onclick = action;
      item.append(button);
    }
    if (savedSide) {
      if (!trackTags.has(frame)) {
        const end = points[index + 1] ?? waveform.frames;
        const matches = Array.isArray(waveform.matches) ? waveform.matches : [];
        const match = matches.find(cue => Number.isSafeInteger(cue?.frame) &&
          cue.frame >= frame && cue.frame < end && cue.tags);
        trackTags.set(frame, match ? {...match.tags} : {});
      }
      const tags = trackTags.get(frame);
      for (const key of ["title", "artist", "album"]) {
        const label = document.createElement("label");
        label.textContent = key.charAt(0).toUpperCase() + key.slice(1) + " ";
        const field = document.createElement("input");
        field.maxLength = 300;
        field.value = tags[key] || "";
        field.oninput = () => { tags[key] = field.value; };
        label.append(field);
        item.append(label);
      }
      const exportButton = document.createElement("button");
      exportButton.textContent = "Export track " + (index + 1) + " as FLAC";
      exportButton.onclick = async () => {
        exportButton.disabled = true;
        try {
          await exportTrack(frame, index);
          el("status").textContent = "Tagged FLAC saved.";
        } catch (error) {
          el("status").textContent = error.message;
        } finally {
          exportButton.disabled = false;
        }
      };
      item.append(exportButton);
    }
    return item;
  }));
}

function reset() {
  points = [];
  trackTags.clear();
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
  }
  points = [...new Set([...points.filter(point => point !== editing), frame])].sort((a, b) => a - b);
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

const side = new URLSearchParams(location.search).get("side");
if (side) loadSavedSide(side);
