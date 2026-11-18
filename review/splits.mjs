export function validateWaveform(data) {
  if (!data || !Number.isSafeInteger(data.frames) || data.frames <= 0 ||
      !Number.isSafeInteger(data.sample_rate) || data.sample_rate <= 0 ||
      !Array.isArray(data.peaks) || !data.peaks.length ||
      data.peaks.length > Math.min(data.frames, 10000) ||
      !data.peaks.every(pair => Array.isArray(pair) && pair.length === 2 &&
        pair.every(peak => Number.isInteger(peak) && peak >= 0 && peak <= 32768))) {
    throw new Error("Choose valid stereo waveform JSON from Record Relay.");
  }
  if (data.suggested_starts !== undefined && (!Array.isArray(data.suggested_starts) ||
      data.suggested_starts.length > 10000 || data.suggested_starts.some((frame, index, starts) =>
        !Number.isSafeInteger(frame) || frame < 0 || frame >= data.frames ||
        (index > 0 && frame <= starts[index - 1])))) {
    throw new Error("Suggested starts must be ordered and within the recording.");
  }
  return data;
}

export function splitPlan(master, waveform, points) {
  const {frames, sample_rate} = validateWaveform(waveform);
  const starts = [...new Set(points)].sort((a, b) => a - b);
  if (starts.some(frame => !Number.isSafeInteger(frame) || frame < 0 || frame >= frames)) {
    throw new Error("Track starts must fall within the recording.");
  }
  return {
    master, frames, sample_rate,
    tracks: starts.map((start, index) => ({start, end: starts[index + 1] ?? frames})),
  };
}
