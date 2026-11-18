import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../', import.meta.url);
test('album workbench is connected to persistent catalog and capture APIs', async () => {
  const [html, client] = await Promise.all([
    readFile(new URL('ui/album.html', root), 'utf8'),
    readFile(new URL('ui/album.mjs', root), 'utf8'),
  ]);
  assert.match(html, /<main id="main">/);
  assert.match(html, /name="control-token"/);
  assert.match(html, /role="status" aria-live="polite"/);
  assert.match(html, /Skip to main content/);
  assert.match(html, /role="group" aria-label="Choose record side"/);
  assert.match(html, /id="project-select"/);
  assert.match(html, /Save recent audio/);
  assert.match(html, /Search MusicBrainz/);
  assert.match(html, /prefers-reduced-motion:reduce/);
  assert.doesNotMatch(html, /Synthetic fixture preview|No recording or catalog changes are sent/);
  for (const endpoint of ['/api/album', '/api/projects', '/api/releases/search', '/api/releases/preview', '/api/releases/pin',
    '/api/takes/start', '/api/takes/stop']) assert.ok(client.includes(endpoint), endpoint);
  assert.match(client, /X-Control-Token/);
  assert.match(client, /record-relay-project/);
  assert.match(client, /record-relay-active-take/);
  assert.match(client, /stopButton/);
});

test('optional visualization exposes static and keyboard-accessible motion paths', async () => {
  const [html, script] = await Promise.all([
    readFile(new URL('ui/album.html', root), 'utf8'),
    readFile(new URL('ui/visualizer.mjs', root), 'utf8'),
  ]);
  assert.match(html, /id="visual-mode"/);
  assert.match(html, /id="visual-delay"[^>]+aria-describedby="visual-delay-help"/);
  assert.match(html, /id="visualizer-dialog" aria-labelledby="visualizer-heading"/);
  assert.match(html, /id="visualizer-canvas" role="img" aria-label=/);
  assert.match(html, /Press Escape or Close visualizer to stop/);
  assert.match(html, /Reduced motion keeps the spectrum static/);
  assert.match(script, /prefers-reduced-motion: reduce/);
  assert.match(script, /document\.hidden/);
  assert.match(script, /packets\.length > 32/);
  assert.match(script, /1000 \/ 24/);
  assert.match(script, /dialog\.close\(\)/);
  assert.doesNotMatch(script, /AudioContext|WebGL|fetch\(/);
});
