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

/** What a step usually involves, shown before a measured estimate exists. */
export const stepHint = (repo) => {
  const index = phaseIndex(repo);
  if (index === 0) return 'Starts as soon as an indexing worker is free.';
  if (index === 1) return 'Downloading the archive from GitHub; large repositories take longer.';
  if (index === 2 || index === 3) return 'Usually takes a few seconds.';
  if (index === 4) return 'Questions unlock when this step finishes.';
  if (index === 5) return 'Answers already work; this improves finding code by meaning.';
  return '';
};
