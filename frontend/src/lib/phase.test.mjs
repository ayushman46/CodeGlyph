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

test('the indexing label names the step in progress; moving counts stay out of it', async () => {
  const { indexingLabel, messageCounts } = await import('./phase.js');
  const base = { is_indexing: true, full_name: 'calcom/cal.com', repo_name: 'cal.com' };
  assert.equal(indexingLabel({ ...base, status: 'queued' }), 'Waiting for an indexing worker');
  assert.equal(indexingLabel({ ...base, status: 'cloning' }), 'Downloading calcom/cal.com');
  assert.equal(indexingLabel({ ...base, status: 'cloning', full_name: '' }), 'Downloading cal.com');
  assert.equal(indexingLabel({ ...base, status: 'scanning' }), 'Selecting the most important files');
  assert.equal(indexingLabel({ ...base, status: 'manifesting', eligible_files: 6398 }), 'Hashing 6,398 selected files');
  assert.equal(indexingLabel({ ...base, status: 'manifesting' }), 'Hashing files and resolving imports');
  assert.equal(indexingLabel({ ...base, status: 'chunking', message: 'Reading source files (4,210 of 6,398 changed files).' }), 'Chunking 6,398 files');
  assert.equal(indexingLabel({ ...base, status: 'chunking', eligible_files: 12000 }), 'Chunking 12,000 files');
  assert.equal(indexingLabel({ ...base, status: 'chunking' }), 'Chunking files into sections');
  assert.equal(indexingLabel({ ...base, status: 'processing', index_phase: 'searchable', semantic_progress: 62 }), 'Building semantic index');
  assert.equal(indexingLabel({ ...base, status: 'embedding', semantic_progress: 140 }), 'Building semantic index');
  assert.equal(indexingLabel({ ...base, status: 'searchable', embedding_status: 'degraded' }), 'Building keyword index');
  assert.equal(indexingLabel({ ...base, status: 'chunking', message: 'Stopping indexing…' }), 'Stopping indexing');
  assert.equal(indexingLabel(null), '');
  assert.deepEqual(messageCounts('Building the semantic index: 1,200 of 4,000 code sections (30%).'), { done: 1200, total: 4000 });
  assert.deepEqual(messageCounts('(9 of 3)'), { done: 3, total: 3 });
  assert.equal(messageCounts('0 of 0'), null);
  assert.equal(messageCounts(undefined), null);
});

test('indexing details report counts, what works, notes and warnings', async () => {
  const { indexingActivity } = await import('./phase.js');
  const chunking = {
    is_indexing: true, status: 'chunking', queryable: false, message: 'Reading source files (4,210 of 6,398 changed files).',
    total_seen_files: 7695, eligible_files: 6398, excluded_files: 1297, excluded_reasons: { repository_budget: 300 },
    indexing_started_at: '2026-10-20T10:00:00+00:00', selection: {},
  };
  const activity = indexingActivity(chunking, { remaining: 'about 3 min left' });
  assert.equal(activity.label, 'Chunking 6,398 files');
  assert.equal(activity.pattern, 'drive');
  assert.equal(activity.startedAt, '2026-10-20T10:00:00+00:00');
  assert.equal(activity.progress, '65% of files read');
  assert.deepEqual(activity.facts, ['7,695 files seen', '6,398 selected', '1,297 skipped']);
  assert.equal(activity.usable, 'Questions unlock when this step finishes');
  assert.equal(activity.remaining, 'about 3 min left');
  assert.deepEqual(activity.notes, ['Large repository: the 6,398 most important files go first; 300 lower-priority files are skipped and listed in the coverage report.']);
  assert.deepEqual(activity.warnings, []);

  const queued = indexingActivity({ is_indexing: true, status: 'queued', indexing_started_at: null });
  assert.equal(queued.pattern, 'pulse');
  assert.equal(queued.startedAt, null);
  assert.deepEqual(queued.facts, []);
  assert.equal(queued.usable, 'Starts as soon as an indexing worker is free');

  const embedding = indexingActivity({
    is_indexing: true, status: 'searchable', queryable: true, semantic_progress: 62, keyword_files: 6398, keyword_chunks: 8120,
    message: 'Building the semantic index: 5,034 of 8,120 code sections (62%). Keyword search already works.',
  });
  assert.equal(embedding.label, 'Building semantic index');
  assert.equal(embedding.progress, '5,034 of 8,120 sections embedded');
  // Without counts in the worker's message, the percentage.
  assert.equal(indexingActivity({ is_indexing: true, status: 'searchable', queryable: true, semantic_progress: 62 }).progress, '62% of sections embedded');
  assert.deepEqual(embedding.facts, ['6,398 indexed', '8,120 sections']);
  assert.equal(embedding.usable, 'Keyword search is ready — you can ask questions now');

  const refresh = indexingActivity({ is_indexing: true, status: 'cloning', queryable: true, refreshing: true });
  assert.equal(refresh.usable, 'Answers use the current index until the refresh finishes');

  const stalled = indexingActivity({ ...chunking, stalled: true, embedding_status: 'degraded', message: '' });
  assert.equal(stalled.warnings.length, 2);
  assert.match(stalled.warnings[0], /hasn't reported progress/);
  // A stalled job waits: the waiting pattern, not the working one.
  assert.equal(stalled.pattern, 'pulse');
  assert.equal(stalled.warnings[1], 'Semantic search is unavailable right now; keyword search still works.');
  // A worker message on screen that already says it: no second warning; without it on screen, the warning stays.
  const degradedRepo = { ...chunking, embedding_status: 'degraded', message: 'Building the keyword index. Semantic search is unavailable right now because the embedding provider did not respond; keyword search works.' };
  assert.deepEqual(indexingActivity(degradedRepo, { messageShown: true }).warnings, []);
  assert.deepEqual(indexingActivity(degradedRepo).warnings, ['Semantic search is unavailable right now; keyword search still works.']);
  // Files indexed so far are read against the selection.
  assert.deepEqual(indexingActivity({ ...chunking, status: 'searchable', keyword_files: 6000 }).facts, ['7,695 files seen', '6,000 of 6,398 files indexed', '1,297 skipped']);

  const stopping = indexingActivity({ ...chunking, message: 'Stopping indexing…' }, { remaining: 'about 3 min left' });
  assert.equal(stopping.label, 'Stopping indexing');
  assert.equal(stopping.pattern, 'pulse');
  assert.equal(stopping.remaining, '');
});

test('BUG-14: the header percentage, the step and the detail line read the same embedded / total', async () => {
  const { indexingActivity, semanticPercent, semanticSections, statusSentence } = await import('./phase.js');
  // The customer's numbers: 258 of 465 sections embedded while the stored
  // percentage (rows written so far as the denominator) said 66%.
  const message = 'Building the semantic index: 258 of 465 code sections (55%). Keyword search already works.';
  const older = { is_indexing: true, status: 'searchable', queryable: true, semantic_progress: 66, message };
  assert.deepEqual(semanticSections(older), { embedded: 258, total: 465 });
  assert.equal(semanticPercent(older), Math.round((258 / 465) * 100));
  assert.equal(phaseLabel(older), 'Ready · semantic 55%');
  assert.equal(indexingActivity(older).progress, '258 of 465 sections embedded');
  assert.equal(statusSentence(older), 'Keyword search ready, semantic index 55%');
  assert.equal(overallProgress(older), Math.round(55 + 55 * 0.44));
  // The API reconciles semantic_progress itself and sends the counts.
  const current = { ...older, semantic_progress: 55, semantic_sections: { embedded: 258, total: 465 } };
  assert.equal(phaseLabel(current), 'Ready · semantic 55%');
  assert.equal(indexingActivity(current).progress, '258 of 465 sections embedded');
  // No counts (degraded provider, a refresh): the stored percentage.
  assert.equal(semanticSections({ ...current, semantic_sections: null }), null);
  assert.equal(indexingActivity({ ...current, semantic_sections: null }).progress, '55% of sections embedded');
  assert.equal(semanticSections({ ...older, refreshing: true }), null);
  assert.equal(semanticSections({ ...older, message: 'Reading source files (4 of 9 changed files).' }), null);
  assert.equal(semanticSections({ ...older, is_indexing: false }), null);
});

test('BUG-14: the step count matches the API (seven steps, Ready included)', async () => {
  const { STEPS, stepPosition } = await import('./phase.js');
  assert.equal(STEPS.length, 7);
  assert.deepEqual(stepPosition({ status: 'queued', is_indexing: true, phase_step: 1, phase_steps: 7 }), { step: 1, steps: 7 });
  assert.deepEqual(stepPosition({ status: 'searchable', is_indexing: true }), { step: 6, steps: 7 });
  assert.deepEqual(stepPosition({ status: 'chunking', is_indexing: true, phase_step: 5, phase_steps: 7 }), { step: 5, steps: 7 });
  assert.deepEqual(stepPosition(null), { step: 1, steps: 7 });
});

test('BUG-14: the file count is what can be searched now, never 0 while keyword search works', async () => {
  const { searchableFiles } = await import('./phase.js');
  // First index: coverage's indexed_files is only written at the end.
  assert.equal(searchableFiles({ is_indexing: true, queryable: true, keyword_files: 56, indexed_files: 0 }), 56);
  // The API's own figure wins.
  assert.equal(searchableFiles({ is_indexing: true, queryable: true, keyword_files: 56, indexed_files: 0, searchable_files: 56 }), 56);
  assert.equal(searchableFiles({ is_indexing: false, queryable: true, keyword_files: 200, indexed_files: 200 }), 200);
  // A legacy index without coverage, and a refresh that reset coverage.
  assert.equal(searchableFiles({ is_indexing: false, queryable: true, keyword_files: 90, indexed_files: 0 }), 90);
  assert.equal(searchableFiles({ is_indexing: true, refreshing: true, queryable: true, keyword_files: 200, indexed_files: 0 }), 200);
  assert.equal(searchableFiles({ is_indexing: true, queryable: false, keyword_files: 0, indexed_files: 0 }), 0);
  assert.equal(searchableFiles(null), 0);
});

test('FEAT-08: a queued repository shows its place in line and the labelled estimate', async () => {
  const { indexingActivity } = await import('./phase.js');
  const queue = {
    ahead: 2, running: 1, position: 3, estimated_wait_seconds: 75, estimate_runs: 7,
    label: '2 repositories ahead of yours · usually starts in about 2 min',
    estimate_note: 'Estimate based on how long the last 7 indexing runs took.',
  };
  const queued = indexingActivity({ is_indexing: true, status: 'queued', queue });
  assert.equal(queued.label, 'Waiting for an indexing worker');
  assert.equal(queued.usable, '2 repositories ahead of yours · usually starts in about 2 min');
  assert.deepEqual(queued.notes, ['Estimate based on how long the last 7 indexing runs took.']);
  // Without queue data the old line stays; once a worker claims it, the queue no longer shows.
  assert.equal(indexingActivity({ is_indexing: true, status: 'queued', queue: null }).usable, 'Starts as soon as an indexing worker is free');
  assert.deepEqual(indexingActivity({ is_indexing: true, status: 'cloning', queue }).notes, []);
});
