import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const versionPattern = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

function parseVersion(value) {
  if (typeof value !== 'string' || !versionPattern.test(value)) return null;
  const parts = value.split('.').map(Number);
  return parts.every(Number.isSafeInteger) ? parts : null;
}

// Intentionally accept only the explicit ^major.minor.patch branches in engines.
// Unsupported syntax fails closed instead of silently admitting an old runtime.
export function supportsNodeVersion(version, engineRange) {
  if (typeof engineRange !== 'string' || !engineRange.trim()) {
    throw new Error('package.json 必须配置 engines.node。');
  }
  const floors = engineRange.split('||').map((branch) => {
    const value = branch.trim();
    const parts = value.startsWith('^') ? parseVersion(value.slice(1)) : null;
    if (!parts || parts[0] === 0) {
      throw new Error('engines.node 仅支持由 || 分隔的 ^major.minor.patch 版本范围。');
    }
    return parts;
  });
  const actual = parseVersion(version);
  if (!actual) return false;
  return floors.some(([major, minor, patch]) => actual[0] === major
    && (actual[1] > minor || (actual[1] === minor && actual[2] >= patch)));
}

function main() {
  try {
    const { engines } = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
    if (supportsNodeVersion(process.versions.node, engines?.node)) return;
    console.error(`Node.js v${process.versions.node} 不满足项目的安全版本要求（${engines.node}）。`);
    console.error('请升级至 Node.js 24.22.1 LTS 或满足上述范围的更新版本后重试。');
    process.exitCode = 1;
  } catch (error) {
    console.error(`无法检查 Node.js v${process.versions.node} 的安全版本要求：${error.message}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main();
