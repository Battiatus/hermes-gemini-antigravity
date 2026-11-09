#!/usr/bin/env node
// Installs the Gemini Antigravity provider into Hermes Agent: copies the plugin into <Hermes home>/plugins/.
// Usage: npx hermes-gemini-antigravity [install|uninstall] [--home <Hermes home>]
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const NAME = 'gemini-antigravity';
const FILES = ['__init__.py', 'antigravity.py', 'client.py', 'plugin.yaml', 'README.md'];
const SOURCE = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Hermes home as Hermes resolves it: --home, then HERMES_HOME, then the platform default. */
export function hermesHome({ argv = process.argv.slice(2), env = process.env, platform = process.platform, home = homedir() } = {}) {
  const i = argv.indexOf('--home');
  if (i >= 0 && argv[i + 1]) return resolve(argv[i + 1]);
  if (env.HERMES_HOME?.trim()) return resolve(env.HERMES_HOME.trim());
  if (platform === 'win32') return join(env.LOCALAPPDATA?.trim() || join(home, 'AppData', 'Local'), 'hermes');
  return join(home, '.hermes');
}

export function install(home) {
  const target = join(home, 'plugins', NAME);
  rmSync(target, { recursive: true, force: true }); // an update replaces the previous version whole
  mkdirSync(target, { recursive: true });
  for (const file of FILES) cpSync(join(SOURCE, file), join(target, file));
  return target;
}

export function uninstall(home) {
  const target = join(home, 'plugins', NAME);
  const found = existsSync(target);
  rmSync(target, { recursive: true, force: true });
  return found ? target : null;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const at = args.indexOf('--home');
  if (at >= 0) args.splice(at, 2);
  const command = args[0] ?? 'install';
  const home = hermesHome();
  if (command === 'uninstall') {
    const removed = uninstall(home);
    console.log(removed ? `Removed ${removed}. Your Antigravity sign-in stays in Hermes until \`hermes auth logout ${NAME}\`.` : `Nothing to remove in ${home}.`);
  } else if (command === 'install') {
    const target = install(home);
    console.log(`Installed the Gemini Antigravity provider in ${target}\n`);
    console.log('Next:');
    console.log(`  hermes auth add ${NAME}     sign in with your Google account`);
    console.log('  hermes model                       pick provider "Gemini Antigravity" and a model');
  } else {
    console.error('Usage: npx hermes-gemini-antigravity [install|uninstall] [--home <Hermes home>]');
    process.exitCode = 1;
  }
}
