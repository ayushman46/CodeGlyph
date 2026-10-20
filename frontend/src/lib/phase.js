export const STEPS = [
  { id: 'queued', label: 'Queued', detail: 'Waiting for the indexing worker.' },
  { id: 'cloning', label: 'Downloading snapshot', detail: 'Streaming the repository archive from GitHub.' },
  { id: 'scanning', label: 'Selecting files', detail: 'Ranking files by importance and applying the indexing budget.' },
  { id: 'manifesting', label: 'Comparing changes', detail: 'Hashing files so unchanged ones are reused.' },
  { id: 'chunking', label: 'Building keyword search', detail: 'Reading code into searchable sections with exact line ranges.' },
  { id: 'searchable', label: 'Semantic index', detail: 'Embedding sections for meaning-based search. You can ask questions now.' },
  { id: 'ready', label: 'Ready', detail: 'Fully indexed.' },
];

export const PHASE_INDEX = { queued: 0, cloning: 1, scanning: 2, manifesting: 3, chunking: 4, keyword: 4, searchable: 5, embedding: 5, ready: 6 };

/** Step at which questions become possible (keyword search is ready after it). */
export const KEYWORD_STEP = PHASE_INDEX.chunking;

/**
 * Step index of a repository. `status` may be a job status ("processing"),
 * so the newer `index_phase` (e.g. "searchable": keyword search ready,
 * semantic index still building) is used when status is not a phase.
 */
export const phaseIndex = (repo) => PHASE_INDEX[repo?.status] ?? PHASE_INDEX[repo?.index_phase] ?? 0;
const isSearchable = (repo) => repo?.status === 'searchable' || repo?.index_phase === 'searchable';
const clampPercent = (value) => Math.max(0, Math.min(100, Number(value) || 0));

export const phaseLabel = (repo) => {
  if (!repo) return '';
  if (repo.status === 'failed') return 'Needs attention';
  if (repo.status === 'cancelled') return 'Stopped';
  if (!repo.is_indexing) {
    const keywordOnly = (repo.semantic_progress < 100 && repo.embedding_status === 'degraded') || isSearchable(repo);
    return keywordOnly ? 'Ready · keyword search' : 'Ready';
  }
  if (repo.refreshing) return `Refreshing · ${STEPS[phaseIndex(repo)]?.label || 'Working'}`;
  if (isSearchable(repo)) return `Ready · semantic ${clampPercent(repo.semantic_progress)}%`;
  return STEPS[phaseIndex(repo)]?.label || 'Indexing';
};

/** True for a finished repository that only has keyword search (semantic index unavailable). */
export const isKeywordOnly = (repo) => Boolean(repo) && !repo.is_indexing && repo.status !== 'failed' && repo.status !== 'cancelled'
  && phaseLabel(repo) === 'Ready · keyword search';

/**
 * Progress inside the current step, read from worker messages such as
 * "Reading source files (120 of 900 changed files)." Null when unknown.
 */
export const phaseFraction = (repo) => {
  const match = /\(\s*([\d,]+)\s+of\s+([\d,]+)/i.exec(String(repo?.message || ''));
  if (!match) return null;
  const done = Number(match[1].replaceAll(',', ''));
  const total = Number(match[2].replaceAll(',', ''));
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null;
  return Math.max(0, Math.min(1, done / total));
};

/** Overall percentage: early phases fill the first half, embedding the rest. */
export const overallProgress = (repo) => {
  if (!repo) return 0;
  if (!repo.is_indexing) return 100;
  const index = phaseIndex(repo);
  if (index >= 5) return Math.round(55 + (clampPercent(repo.semantic_progress) * 0.44));
  // A step that reports "N of M" moves the bar within its own share.
  const within = index > 0 ? (phaseFraction(repo) ?? 0) : 0;
  return Math.round(((index + within) / 5) * 55);
};

/** Badge tone for a repository's state. */
export const statusTone = (repo) => {
  if (repo?.status === 'failed') return 'danger';
  if (repo?.status === 'cancelled') return 'neutral';
  if (repo?.is_indexing) return 'warning';
  return 'success';
};

/** Short state for screen readers and lists, e.g. "Indexing, 44%". */
export const statusSentence = (repo) => {
  if (!repo) return '';
  if (repo.status === 'failed') return 'Indexing failed';
  if (repo.status === 'cancelled') return 'Indexing stopped';
  if (repo.is_indexing) {
    if (repo.queryable) return `Keyword search ready, semantic index ${clampPercent(repo.semantic_progress)}%`;
    return `Indexing, ${overallProgress(repo)}%`;
  }
  return isKeywordOnly(repo) ? 'Ready, keyword search only' : 'Ready';
};

/**
 * Progress samples for an ETA. Each sample is { key, fraction, at } where
 * `key` names the measured step; a new step (or progress going backwards)
 * starts over. Samples older than `windowMs` are dropped, keeping two.
 */
export const trackProgress = (samples, { key, fraction, at }, windowMs = 180000) => {
  if (fraction == null || !Number.isFinite(fraction)) return [];
  const value = Math.max(0, Math.min(1, fraction));
  const last = samples[samples.length - 1];
  if (!last || last.key !== key || value < last.fraction) return [{ key, fraction: value, at }];
  if (at - last.at < 1000) return samples;
  const next = [...samples, { key, fraction: value, at }];
  const recent = next.filter((sample) => at - sample.at <= windowMs);
  return recent.length >= 2 ? recent : next.slice(-2);
};

/** Milliseconds left at the observed pace, or null until it is measurable. */
export const estimateRemainingMs = (samples, minSpanMs = 10000) => {
  if (!Array.isArray(samples) || samples.length < 2) return null;
  const first = samples[0];
  const last = samples[samples.length - 1];
  const span = last.at - first.at;
  const gained = last.fraction - first.fraction;
  if (last.fraction >= 1) return 0;
  if (span < minSpanMs || gained <= 0) return null;
  return ((1 - last.fraction) * span) / gained;
};

/** "about 3 min left" style label; empty when there is no estimate. */
export const remainingLabel = (ms) => {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '';
  if (ms < 45000) return 'less than a minute left';
  const minutes = Math.ceil(ms / 60000);
  if (minutes < 60) return `about ${minutes} min left`;
  return 'more than an hour left';
};

// ---------------------------------------------------------------------------
// Live indexing activity (the Loading State line): what is being done now,
// in words and exact counts, read only from fields the status poll returns.

const exactNumber = new Intl.NumberFormat('en-US');

/** An exact count with grouping ("6,398"); live progress never rounds to "6.4k". */
export const formatExact = (value) => exactNumber.format(Math.max(0, Math.round(Number(value) || 0)));

const positive = (value) => {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
};

/**
 * The first "N of M" pair in a worker message, e.g. "Reading source files
 * (4,210 of 6,398 changed files)." or "Building the semantic index: 1,200 of
 * 4,000 code sections (30%)." Null when the message reports no counts.
 * @param {unknown} message
 * @returns {{ done: number, total: number } | null}
 */
export const messageCounts = (message) => {
  const match = /([\d,]+)\s+of\s+([\d,]+)/i.exec(String(message || ''));
  if (!match) return null;
  const done = Number(match[1].replaceAll(',', ''));
  const total = Number(match[2].replaceAll(',', ''));
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null;
  return { done: Math.min(done, total), total };
};

/** Files the importance budget left out: from the published selection, or the live coverage reasons. */
export const budgetOmittedFiles = (repo) => positive(repo?.selection?.budget_omitted_files)
  || positive(repo?.excluded_reasons?.repository_budget);

const isStopping = (repo) => Boolean(repo?.is_indexing) && /^stopping/i.test(String(repo?.message || ''));
const STALLED_DETAIL = "Indexing hasn't reported progress for a few minutes. It resumes automatically if the worker restarted; if it stays stuck, stop it and re-index.";

/**
 * The work in progress, written as the work ("Chunking 6,398 files",
 * "Building semantic index"), never "Loading". This is the Loading State's
 * announced status, so it names the step and changes only when the step
 * does: the moving counts ("4,210 of 6,398", "62%") go in the details
 * (`indexingActivity().progress`), which are not announced on every poll.
 * @param {object} repo A repository from the status poll.
 * @returns {string}
 */
export const indexingLabel = (repo) => {
  if (!repo) return '';
  if (isStopping(repo)) return 'Stopping indexing';
  const index = phaseIndex(repo);
  const name = repo.full_name || repo.repo_name || 'the repository';
  const selected = positive(repo.eligible_files);
  if (index === 0) return 'Waiting for an indexing worker';
  if (index === 1) return `Downloading ${name}`;
  if (index === 2) return 'Selecting the most important files';
  if (index === 3) return selected ? `Hashing ${formatExact(selected)} selected files` : 'Hashing files and resolving imports';
  if (index === 4) {
    const counts = messageCounts(repo.message);
    if (counts) return `Chunking ${formatExact(counts.total)} files`;
    return selected ? `Chunking ${formatExact(selected)} files` : 'Chunking files into sections';
  }
  if (index >= 5) {
    if (repo.embedding_status === 'degraded') return 'Building keyword index';
    return 'Building semantic index';
  }
  return 'Indexing';
};

/**
 * Everything the Loading State shows for an indexing repository.
 *
 * - `label`: the announced status (see `indexingLabel`).
 * - `pattern`: `pulse` while waiting (queued, stopping, stalled), `drive` while working.
 * - `startedAt`: when the worker claimed the job (`indexing_started_at`), so
 *   the elapsed time survives reloads; null (count from mount) while queued.
 * - `facts`: exact live counts (files seen, selected, indexed, sections, skipped).
 * - `progress`: progress inside the step (the moving count the label leaves out).
 * - `usable`: what already works, or when questions unlock.
 * - `notes`: informational lines (the importance budget).
 * - `warnings`: problems worth attention (a stalled worker, semantic search unavailable).
 *
 * @param {object} repo A repository from the status poll.
 * @param {{ remaining?: string, messageShown?: boolean }} [options]
 *   `remaining`: a measured time left for this step ("about 3 min left").
 *   `messageShown`: the worker's message is already on screen, so a warning
 *   it already states is not repeated.
 */
export const indexingActivity = (repo, { remaining = '', messageShown = false } = {}) => {
  const index = phaseIndex(repo);
  const stopping = isStopping(repo);
  const queryable = Boolean(repo?.queryable);
  const counts = messageCounts(repo?.message);

  const facts = [];
  const seen = positive(repo?.total_seen_files);
  const selected = positive(repo?.eligible_files);
  const indexed = index >= 5 ? positive(repo?.keyword_files) : 0;
  const sections = positive(repo?.keyword_chunks) || positive(repo?.chunk_count);
  const skipped = positive(repo?.excluded_files);
  if (seen) facts.push(`${formatExact(seen)} files seen`);
  if (indexed && selected) facts.push(`${formatExact(Math.min(indexed, selected))} of ${formatExact(selected)} files indexed`);
  else if (indexed) facts.push(`${formatExact(indexed)} indexed`);
  else if (selected) facts.push(`${formatExact(selected)} selected`);
  if (sections) facts.push(`${formatExact(sections)} ${sections === 1 ? 'section' : 'sections'}`);
  if (skipped) facts.push(`${formatExact(skipped)} skipped`);

  let progress = '';
  if (index === 4 && counts) progress = `${Math.floor((counts.done / counts.total) * 100)}% of files read`;
  else if (index >= 5 && repo?.embedding_status !== 'degraded') {
    progress = counts
      ? `${formatExact(counts.done)} of ${formatExact(counts.total)} sections embedded`
      : `${clampPercent(repo?.semantic_progress)}% of sections embedded`;
  }

  let usable;
  if (stopping) usable = 'Stopping takes a few seconds';
  else if (repo?.refreshing) usable = 'Answers use the current index until the refresh finishes';
  else if (queryable) usable = 'Keyword search is ready — you can ask questions now';
  else if (index === 4) usable = 'Questions unlock when this step finishes';
  else if (index === 0) usable = 'Starts as soon as an indexing worker is free';
  else usable = 'Questions unlock once keyword search is built';

  const notes = [];
  const omitted = budgetOmittedFiles(repo);
  if (omitted && selected) {
    notes.push(`Large repository: the ${formatExact(selected)} most important files go first; ${formatExact(omitted)} lower-priority files are skipped and listed in the coverage report.`);
  }

  const warnings = [];
  if (repo?.stalled) warnings.push(STALLED_DETAIL);
  const messageSaysIt = messageShown && /semantic search is unavailable/i.test(String(repo?.message || ''));
  if (repo?.is_indexing && repo?.embedding_status === 'degraded' && !messageSaysIt) {
    warnings.push('Semantic search is unavailable right now; keyword search still works.');
  }

  return {
    label: indexingLabel(repo),
    // Waiting (queued, stopping, or a worker that stopped reporting) pulses; work drives.
    pattern: stopping || index === 0 || repo?.stalled ? 'pulse' : 'drive',
    startedAt: repo?.indexing_started_at || null,
    facts,
    progress,
    usable,
    remaining: stopping ? '' : remaining,
    notes,
    warnings,
  };
};
