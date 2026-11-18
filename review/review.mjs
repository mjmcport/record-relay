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

function ready() {
  return Boolean(master && waveform && Number.isFinite(player.duration) &&
    Math.abs(player.duration - waveform.frames / waveform.sample_rate) <= .015);
}

function render() {
  const usable = ready();
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
  el("splits").replaceChildren(...points.map(frame => {
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
    return item;
  }));
}

function reset() {
  points = [];
  editing = undefined;
  el("seconds").value = "0";
  el("status").textContent = "Waiting for a matching recording and waveform.";
}

el("recording").onchange = event => {
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
