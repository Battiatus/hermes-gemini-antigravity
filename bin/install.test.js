import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { hermesHome, install, uninstall } from './install.js';

test('Hermes home follows --home, HERMES_HOME, then the platform default', () => {
  assert.equal(hermesHome({ argv: ['--home', '/x/h'], env: {}, platform: 'linux', home: '/u' }), resolve('/x/h'));
  assert.equal(hermesHome({ argv: [], env: { HERMES_HOME: '/e/h' }, platform: 'linux', home: '/u' }), resolve('/e/h'));
  assert.equal(hermesHome({ argv: [], env: {}, platform: 'darwin', home: '/u' }), join('/u', '.hermes'));
  assert.equal(hermesHome({ argv: [], env: { LOCALAPPDATA: 'C:\\L' }, platform: 'win32', home: 'C:\\u' }), join('C:\\L', 'hermes'));
});

test('install copies the plugin and uninstall removes it', () => {
  const home = mkdtempSync(join(tmpdir(), 'hermes-home-'));
  try {
    const target = install(home);
    assert.match(readFileSync(join(target, 'plugin.yaml'), 'utf8'), /kind: model-provider/);
    assert.ok(existsSync(join(target, 'antigravity.py')) && existsSync(join(target, 'client.py')));
    assert.equal(uninstall(home), target);
    assert.equal(existsSync(target), false);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
