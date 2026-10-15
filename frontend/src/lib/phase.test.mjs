import assert from 'node:assert/strict';
import test from 'node:test';
import { overallProgress, phaseIndex, phaseLabel } from './phase.js';

test('the searchable phase (keyword ready, semantic building) is understood from status or index_phase', () => {
  const viaPhase = { status: 'processing', index_phase: 'searchable', is_indexing: true, semantic_progress: 40 };
  assert.equal(phaseIndex(viaPhase), 5);
  assert.equal(phaseLabel(viaPhase), 'Ready · semantic 40%');
  assert.equal(phaseLabel({ status: 'searchable', is_indexing: true, semantic_progress: 7 }), 'Ready · semantic 7%');
  assert.equal(overallProgress(viaPhase), Math.round(55 + 40 * 0.44));
  assert.equal(phaseLabel({ status: 'ready', index_phase: 'searchable', is_indexing: false }), 'Ready · keyword search');
  assert.equal(phaseLabel({ status: 'ready', index_phase: 'ready', is_indexing: false, semantic_progress: 100 }), 'Ready');
  assert.equal(phaseLabel({ status: 'chunking', is_indexing: true }), 'Building keyword search');
  assert.equal(phaseLabel({ status: 'failed' }), 'Needs attention');
});

test('a step that reports "N of M" moves the overall bar within its share', async () => {
  const { phaseFraction } = await import('./phase.js');
  const chunking = { status: 'chunking', is_indexing: true, message: 'Reading source files (120 of 900 changed files).' };
  assert.equal(phaseFraction(chunking), 120 / 900);
  assert.equal(overallProgress(chunking), Math.round(((4 + 120 / 900) / 5) * 55));
  assert.equal(phaseFraction({ message: 'Reading (1,200 of 2,400 files)' }), 0.5);
  assert.equal(phaseFraction({ message: 'Waiting' }), null);
  assert.equal(overallProgress({ status: 'chunking', is_indexing: true }), 44);
  // A finished step never overtakes the next one.
  assert.equal(overallProgress({ ...chunking, message: '(900 of 900)' }), 55);
  assert.equal(overallProgress({ status: 'queued', is_indexing: true, message: '(3 of 4)' }), 0);
});

test('time-left estimates come only from observed progress', async () => {
  const { estimateRemainingMs, remainingLabel, trackProgress } = await import('./phase.js');
  let samples = trackProgress([], { key: 'a:5', fraction: 0.2, at: 0 });
  assert.equal(estimateRemainingMs(samples), null);
  samples = trackProgress(samples, { key: 'a:5', fraction: 0.3, at: 30000 });
  // 10% per 30 s with 70% to go: 210 s.
  assert.equal(Math.round(estimateRemainingMs(samples)), 210000);
  assert.equal(remainingLabel(estimateRemainingMs(samples)), 'about 4 min left');
  // A stall slows the estimate down instead of keeping the old pace.
  samples = trackProgress(samples, { key: 'a:5', fraction: 0.3, at: 60000 });
  assert.equal(Math.round(estimateRemainingMs(samples)), 420000);
  // A new step (or a reset) starts over.
  assert.equal(trackProgress(samples, { key: 'a:6', fraction: 0.1, at: 61000 }).length, 1);
  assert.equal(trackProgress(samples, { key: 'a:5', fraction: 0.1, at: 61000 }).length, 1);
  assert.deepEqual(trackProgress(samples, { key: 'a:5', fraction: null, at: 61000 }), []);
  // Old samples fall out of the window but two are always kept.
  const old = trackProgress(trackProgress([], { key: 'k', fraction: 0.1, at: 0 }), { key: 'k', fraction: 0.2, at: 400000 });
  assert.equal(old.length, 2);
  assert.equal(remainingLabel(null), '');
  assert.equal(remainingLabel(20000), 'less than a minute left');
  assert.equal(remainingLabel(4 * 3600000), 'more than an hour left');
});

test('status sentences and tones describe every state', async () => {
  const { isKeywordOnly, statusSentence, statusTone } = await import('./phase.js');
  assert.equal(statusSentence({ status: 'failed' }), 'Indexing failed');
  assert.equal(statusSentence({ status: 'cancelled' }), 'Indexing stopped');
  assert.equal(statusSentence({ status: 'searchable', is_indexing: true, queryable: true, semantic_progress: 42 }), 'Keyword search ready, semantic index 42%');
  assert.equal(statusSentence({ status: 'chunking', is_indexing: true }), 'Indexing, 44%');
  assert.equal(statusSentence({ status: 'ready', is_indexing: false, semantic_progress: 100 }), 'Ready');
  assert.equal(isKeywordOnly({ status: 'ready', is_indexing: false, semantic_progress: 60, embedding_status: 'degraded' }), true);
  assert.equal(statusSentence({ status: 'ready', is_indexing: false, semantic_progress: 60, embedding_status: 'degraded' }), 'Ready, keyword search only');
  assert.equal(statusTone({ status: 'failed' }), 'danger');
  assert.equal(statusTone({ status: 'cancelled' }), 'neutral');
  assert.equal(statusTone({ status: 'chunking', is_indexing: true }), 'warning');
  assert.equal(statusTone({ status: 'ready' }), 'success');
});
