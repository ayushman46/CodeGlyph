/*
 * Pure logic for the Context Meter (components/ai/ContextMeter.jsx): the
 * ratio, the percentage it shows, its status and the label presets. The
 * meter counts nothing itself; callers feed it the figures the API returns.
 */

/** @typedef {'ok' | 'near' | 'full'} ContextMeterStatus */

/**
 * One row of the details list: a label and its count, in whatever unit
 * `labels.number` formats (tokens by default, bytes with `bytesLabels`).
 * @typedef {{ label: string, tokens: number }} ContextMeterPart
 */

/**
 * Every string the meter shows. `number` formats every count (used, limit,
 * remaining and each part).
 * @typedef {object} ContextMeterLabels
 * @property {string} title Details popover heading.
 * @property {(percent: number) => string} summary The trigger's accessible name.
 * @property {(used: string, limit: string) => string} tokens Counts line, also the bar's aria-valuetext.
 * @property {(remaining: string) => string} remaining "N left".
 * @property {string} near Status word at or past `nearAt`.
 * @property {string} full Status word at or past the limit.
 * @property {string} cost Label of the cost row.
 * @property {(value: number) => string} number Formats every count.
 */

const numberFormat = new Intl.NumberFormat('en-US');

/** @type {ContextMeterLabels} */
export const defaultContextMeterLabels = {
  title: 'Context window',
  summary: (percent) => `${percent}% of context used`,
  tokens: (used, limit) => `${used} of ${limit} tokens`,
  remaining: (tokens) => `${tokens} left`,
  near: 'Near the limit',
  full: 'At the limit',
  cost: 'Cost',
  number: (value) => numberFormat.format(value),
};

/**
 * Bytes at one shared precision: one decimal at every size ("171.4 MB",
 * "28.6 MB", "200 MB"), so used, limit and remaining add up. The app's
 * `formatBytes` drops the decimal from 100 up, which would print
 * "171 MB of 200 MB" beside "28.6 MB left".
 * @param {number} value bytes
 * @returns {string}
 */
export const formatBytesFixed = (value) => {
  const bytes = Math.round(Math.max(0, Number(value) || 0));
  if (bytes < 1000) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  const tenths = (amount) => Math.round(amount * 10) / 10;
  let amount = bytes / 1000;
  let index = 0;
  // The unit is chosen after rounding: 999,960 B is "1 MB", never "1000 KB".
  while (tenths(amount) >= 1000 && index < units.length - 1) {
    amount /= 1000;
    index += 1;
  }
  return `${tenths(amount).toFixed(1).replace(/\.0$/, '')} ${units[index]}`;
};

/**
 * Storage preset: indexed source bytes against the plan's quota
 * ("22 MB of 200 MB", "178 MB left"), every count at one decimal
 * (`formatBytesFixed`), so the figures add up. Parts are bytes too (e.g.
 * one row per repository).
 * @type {ContextMeterLabels}
 */
export const bytesLabels = {
  ...defaultContextMeterLabels,
  title: 'Indexed source',
  summary: (percent) => `${percent}% of indexed source used`,
  tokens: (used, limit) => `${used} of ${limit}`,
  number: (value) => formatBytesFixed(value),
};

/**
 * File budget preset: a repository's indexed files against its file
 * budget ("9,412 of 10,000 files").
 * @type {ContextMeterLabels}
 */
export const filesLabels = {
  ...defaultContextMeterLabels,
  title: 'Files indexed',
  summary: (percent) => `${percent}% of the file budget used`,
  tokens: (used, limit) => `${used} of ${limit} files`,
};

/** Ring geometry: a 16px viewBox, radius 6, stroke 2. */
export const RING_RADIUS = 6;
export const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
/** Seconds a change in used/limit takes to glide. */
export const CHANGE_SECONDS = 0.6;
/** Default share of the limit from which the meter reads "near". */
export const DEFAULT_NEAR_AT = 0.8;

// Absorbs binary rounding in `ratio * 100` (0.29 * 100 is 28.999999999999996),
// far below one percent of any real count.
const PERCENT_EPSILON = 1e-9;

/** Used count as the meter reads it: never below zero (non-numbers read 0). */
export const clampUsed = (used) => {
  const value = Number(used);
  return Number.isFinite(value) ? Math.max(value, 0) : 0;
};

/** used / limit, or 0 when there is no positive limit. May exceed 1. */
export const meterRatio = (used, limit) => {
  const max = Number(limit);
  return Number.isFinite(max) && max > 0 ? clampUsed(used) / max : 0;
};

/**
 * Whole percent shown for a ratio. Rounds down below the limit, so 99.6%
 * reads 99% and "100%" appears only when the limit is reached; at or past
 * the limit it is the real figure (104%).
 * @param {number} ratio
 * @returns {number}
 */
export const toPercent = (ratio) => {
  if (!Number.isFinite(ratio) || ratio <= 0) return 0;
  if (ratio >= 1) return Math.round(ratio * 100);
  return Math.min(99, Math.floor(ratio * 100 + PERCENT_EPSILON));
};

/**
 * ok below `nearAt`, near from `nearAt`, full from the limit.
 * @param {number} ratio
 * @param {number} [nearAt=0.8]
 * @returns {ContextMeterStatus}
 */
export const meterStatus = (ratio, nearAt = DEFAULT_NEAR_AT) => {
  if (ratio >= 1) return 'full';
  if (ratio >= nearAt) return 'near';
  return 'ok';
};

/** The status word for near/full, null when ok. */
export const statusLabelFor = (status, labels = defaultContextMeterLabels) => {
  if (status === 'full') return labels.full;
  if (status === 'near') return labels.near;
  return null;
};

/** Count left before the limit, never negative. */
export const remainingOf = (used, limit) => Math.max((Number(limit) || 0) - clampUsed(used), 0);

/** stroke-dasharray of the ring for a ratio (capped full past the limit). */
export const ringDashFor = (ratio) => `${Math.min(Math.max(ratio, 0), 1) * RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`;

/** Width of the bar fill for a ratio (capped full past the limit). */
export const barWidthFor = (ratio) => `${Math.min(Math.max(ratio, 0), 1) * 100}%`;

/** Merges a partial labels object over the defaults (the reference's `{ ...defaults, ...labels }`). */
export const resolveLabels = (labels) => ({ ...defaultContextMeterLabels, ...labels });
