import assert from 'node:assert/strict';
import test from 'node:test';
import { supportsNodeVersion } from './check-node.mjs';

const range = '^22.23.4 || ^24.22.1 || ^26.12.1';

test('each supported branch requires its October 2026 security patch', () => {
  for (const version of ['22.23.4', '24.22.1', '26.12.1']) {
    assert.equal(supportsNodeVersion(version, range), true, version);
  }
  for (const version of ['22.23.3', '24.22.0', '26.12.0']) {
    assert.equal(supportsNodeVersion(version, range), false, version);
  }
});

test('old, end-of-life and undeclared future branches are rejected', () => {
  for (const version of ['24.19.0', '22.12.0', '20.19.0', '23.99.0', '25.99.0', '27.0.0', '28.0.0']) {
    assert.equal(supportsNodeVersion(version, range), false, version);
  }
});

test('later patches and minor versions stay within their declared major', () => {
  for (const version of ['22.23.5', '22.24.0', '24.22.2', '24.23.0', '26.12.2', '26.13.0']) {
    assert.equal(supportsNodeVersion(version, range), true, version);
  }
  for (const version of ['22.22.999', '24.21.999', '26.11.999']) {
    assert.equal(supportsNodeVersion(version, range), false, version);
  }
});

test('prereleases and malformed runtime versions are rejected', () => {
  for (const version of ['24.22.1-rc.1', '24.23.0-nightly.1', 'v24.22.1', '24.22', '024.22.1', '24.22.1+custom', '', null]) {
    assert.equal(supportsNodeVersion(version, range), false, String(version));
  }
});

test('the supplied engines range controls the allowed branches and floors', () => {
  assert.equal(supportsNodeVersion('24.22.1', '^24.23.0'), false);
  assert.equal(supportsNodeVersion('24.23.0', ' ^24.23.0 '), true);
  assert.equal(supportsNodeVersion('22.23.4', '^24.22.1'), false);
});

test('unsupported engine syntax fails closed', () => {
  for (const invalid of [undefined, '', '>=22.12.0', '^24.22', '^24.22.1 ||', '^0.1.0']) {
    assert.throws(() => supportsNodeVersion('24.22.1', invalid));
  }
});
