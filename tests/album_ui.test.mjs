import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../', import.meta.url);
test('album workbench exposes fixture-backed, honest capture and album controls', async () => {
  const [html, client, album, capture] = await Promise.all([
    readFile(new URL('ui/album.html', root), 'utf8'),
    readFile(new URL('ui/album.mjs', root), 'utf8'),
    readFile(new URL('tests/fixtures/p0/album_compilation_ab.json', root), 'utf8').then(JSON.parse),
    readFile(new URL('tests/fixtures/p0/capture_snapshot_v1.json', root), 'utf8').then(JSON.parse),
  ]);
  assert.match(html, /<main id="main">/);
  assert.match(html, /Synthetic fixture preview/);
  assert.match(html, /<fieldset><legend>Choose the release/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /Illustrative waveform for the current fixture side/);
  assert.equal(album.album_artist, 'Various Artists');
  assert.notEqual(album.takes[1].tracks[0].performer, album.album_artist);
  assert.equal(capture.side, 'B');
  assert.equal(capture.state, 'recording');
  assert.match(client, /accepted_frames/);
  assert.match(client, /No recording action was sent/);
});
