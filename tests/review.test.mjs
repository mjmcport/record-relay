import assert from "node:assert/strict";
import { test } from "node:test";
import { splitPlan, validateWaveform } from "../review/splits.mjs";

const waveform = {sample_rate: 48000, frames: 10, peaks: [[1, 2]]};

test("sorts and deduplicates inclusive starts with exclusive ends", () => {
  assert.deepEqual(splitPlan("test.wav", waveform, [6, 0, 6]).tracks,
    [{start: 0, end: 6}, {start: 6, end: 10}]);
});

test("rejects out-of-range and non-integer positions", () => {
  for (const point of [-1, 10, 2.5, NaN]) {
    assert.throws(() => splitPlan("test.wav", waveform, [point]));
  }
});

test("rejects malformed stereo peaks", () => {
  assert.throws(() => validateWaveform({...waveform, peaks: [[32769, 0]]}));
});
