import assert from 'node:assert/strict';
import test from 'node:test';
import { scoreboardProblemTotals } from '../frontend/src/lib/scoreboard.js';

function contest(stats = [], published) {
  return {
    canRead: true, problems: [{ id: 1 }, { id: 2 }],
    part: { ruleset: 'ICPC', scoreboard_problem_totals: published },
    state: { demo: false, scoreboard: stats.map(stat => ({ problem_stats: { 1: stat } })) },
  };
}

test('count submissions rather than distinct teams and suppress frozen solve flags', () => {
  const c = contest([
    { solved: true, attempts: 4, display: '+3\n0:14' },
    { solved: false, attempts: 6, display: '-6' },
    { solved: true, pending: true, attempts: 3, display: '?3' },
  ]);
  assert.equal(scoreboardProblemTotals(c)[1].text, '1 / 13');
  assert.equal(scoreboardProblemTotals(c)[2].text, '0 / 0');
});

test('native header totals take priority over partial displayed rows', () => {
  const c = contest([{ solved: true, attempts: 2 }], { 1: { solved: 24, submissions: 36 } });
  assert.equal(scoreboardProblemTotals(c)[1].text, '24 / 36');
  c.part.scoreboard_problem_totals[1] = { solved: null, submissions: null };
  assert.equal(scoreboardProblemTotals(c)[1].text, '— / —');
});

test('legacy PC2 and external formats retain their visible attempt counts', () => {
  const c = contest([
    { solved: true, display: '2/14' }, { pending: true, display: '?3' },
    { failed: true, display: '-5' }, { solved: true, display: '+\n0:14' },
    { display: '0/--' },
  ]);
  assert.equal(scoreboardProblemTotals(c)[1].text, '2 / 11');
});

test('unpublished or invalid counts are never silently shown as zero', () => {
  const c = contest([{ pending: true, display: '?' }, { solved: true, attempts: 2 }]);
  assert.equal(scoreboardProblemTotals(c)[1].text, '1 / —');
  c.part.scoreboard_problem_totals = { 1: { solved: true, submissions: Number.MAX_SAFE_INTEGER + 1 } };
  assert.equal(scoreboardProblemTotals(c)[1].text, '— / —');
});

test('summary obeys contest gates and scoreboard errors', () => {
  const c = contest([{ solved: true, attempts: 2 }], { 1: { solved: 24, submissions: 36 } });
  c.canRead = false;
  assert.deepEqual(scoreboardProblemTotals(c), {});
  c.canRead = true;
  c.part.scoreboard_available = false;
  assert.deepEqual(scoreboardProblemTotals(c), {});
  c.part.scoreboard_available = true;
  c.state.scoreboard = [{} , null];
  assert.deepEqual(scoreboardProblemTotals(c), {});
});

test('demo summaries use the visible whole-board results', () => {
  const c = contest([{ solved: true, attempts: 2, submissions: 4, pending: 1 }, { solved: false, attempts: 3, pending: 2 }]);
  c.state.demo = true;
  assert.equal(scoreboardProblemTotals(c)[1].text, '1 / 7');
});
