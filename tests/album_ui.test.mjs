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
