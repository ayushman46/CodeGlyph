/**
 * Pure logic for the Loading State (components/ai/LoadingState.jsx), ported
 * from Honest UI's Loading State: the seven mark patterns (each cell's delay
 * in the cycle), the cell styles, and the elapsed-time label.
 *
 * The keyframes live in index.css (`ci-loading-cell`, `ci-loading-pulse`),
 * together with the reduced-motion (no animation) and forced-colors
 * (CanvasText cells) rules for `.ci-loading-cell`.
 */

/** @typedef {'drive'|'dots'|'orbit'|'diagonal'|'diamond'|'square'|'pulse'} LoadingPattern */

/**
 * @typedef {object} LoadingPatternConfig
 * @property {ReadonlyArray<number|null>} delays  each cell's animation delay in ms; `null` leaves a cell unlit
 * @property {number} durationMs  length of one cycle
 * @property {boolean} [isRound]  round cells instead of 1px-rounded squares
 * @property {boolean} [hideEmpty]  hide unlit cells instead of drawing them very dim
 * @property {string} [animationName]  keyframes name (defaults to `ci-loading-cell`)
 */

/**
 * @typedef {object} ResolvedLoadingPattern
 * @property {LoadingPattern} name
 * @property {ReadonlyArray<number|null>} delays
 * @property {number} durationMs
 * @property {boolean} isRound
 * @property {boolean} hideEmpty
 * @property {string} animationName
 */

export const LOADING_CELL_ANIMATION = 'ci-loading-cell';
export const LOADING_PULSE_ANIMATION = 'ci-loading-pulse';
/** Opacity of a cell that takes part in the cycle while it rests (and under reduced motion). */
export const LIT_CELL_OPACITY = 0.15;
/** Opacity of a cell left out of the cycle (e.g. the dim orbit center). */
export const UNLIT_CELL_OPACITY = 0.08;
export const DEFAULT_LOADING_PATTERN = 'drive';

const freeze = (values) => Object.freeze(values);

/** Wave out from the middle of the left column: (column + |row - 1|) * 90ms. */
const waveDelays = freeze(Array.from({ length: 9 }, (_, index) => {
  const row = Math.floor(index / 3);
  const column = index % 3;
  return (column + Math.abs(row - 1)) * 90;
}));

/** Grid indices clockwise around the edge, from the top-left corner. */
export const PERIMETER = freeze([0, 1, 2, 5, 8, 7, 6, 3]);

const orbitDelays = freeze(Array.from({ length: 9 }, (_, index) => {
  const position = PERIMETER.indexOf(index);
  return position === -1 ? null : position * 110;
}));

/** @type {Readonly<Record<LoadingPattern, Readonly<LoadingPatternConfig>>>} */
export const LOADING_PATTERNS = freeze({
  drive: freeze({ delays: waveDelays, durationMs: 650, isRound: false }),
  dots: freeze({ delays: waveDelays, durationMs: 650, isRound: true }),
  orbit: freeze({ delays: orbitDelays, durationMs: 950, isRound: false }),
  diagonal: freeze({
    delays: freeze(Array.from({ length: 9 }, (_, index) => -1200 + (Math.floor(index / 3) + (index % 3)) * 150)),
    durationMs: 1200,
  }),
  // Top, right, bottom and left around a center: the corners are unlit and hidden.
  diamond: freeze({ delays: freeze([null, -1200, null, -300, -750, -900, null, -600, null]), durationMs: 1200, hideEmpty: true }),
  square: freeze({
    delays: freeze(Array.from({ length: 9 }, (_, index) => {
      const position = PERIMETER.indexOf(index);
      return position === -1 ? null : -1200 + position * 150;
    })),
    durationMs: 1200,
    hideEmpty: true,
  }),
  pulse: freeze({ delays: freeze([-1200, -1000, -800]), durationMs: 1200, animationName: LOADING_PULSE_ANIMATION }),
});

/** @type {ReadonlyArray<LoadingPattern>} */
export const LOADING_PATTERN_NAMES = freeze(/** @type {LoadingPattern[]} */ (Object.keys(LOADING_PATTERNS)));

/**
 * A pattern with its defaults filled in. An unknown name falls back to
 * `drive` (the reference would throw on it).
 *
 * @param {string} [pattern]
 * @returns {ResolvedLoadingPattern}
 */
export function resolveLoadingPattern(pattern = DEFAULT_LOADING_PATTERN) {
  const name = /** @type {LoadingPattern} */ (
    Object.prototype.hasOwnProperty.call(LOADING_PATTERNS, pattern) ? pattern : DEFAULT_LOADING_PATTERN
  );
  const { delays, durationMs, isRound = false, hideEmpty = false, animationName = LOADING_CELL_ANIMATION } = LOADING_PATTERNS[name];
  return { name, delays, durationMs, isRound, hideEmpty, animationName };
}

/**
 * Inline style of one mark cell: resting opacity, hidden when unlit in a
 * `hideEmpty` pattern, and the keyframe animation for lit cells.
 *
 * @param {number|null} delay
 * @param {{ durationMs: number, hideEmpty?: boolean, animationName?: string }} pattern
 * @returns {{ opacity: number, visibility: 'hidden'|undefined, animation: string|undefined }}
 */
export function loadingCellStyle(delay, { durationMs, hideEmpty = false, animationName = LOADING_CELL_ANIMATION }) {
  const isUnlit = delay === null;
  return {
    opacity: isUnlit ? UNLIT_CELL_OPACITY : LIT_CELL_OPACITY,
    visibility: isUnlit && hideEmpty ? 'hidden' : undefined,
    animation: isUnlit ? undefined : `${animationName} ${durationMs}ms ease-in-out ${delay}ms infinite`,
  };
}

/**
 * Elapsed time as `42s`, then `1m 12s` (minutes keep counting past an hour:
 * `75m 3s`). Negative times (a start in the future, e.g. clock skew) read
 * `0s`, and so does a value that is not a finite number.
 *
 * @param {number} milliseconds
 * @returns {string}
 */
export function formatElapsed(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (!Number.isFinite(seconds)) return '0s';
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

// "2026-10-20T12:00:00", "2026-10-20 12:00:00.123456": no zone designator.
const NAIVE_TIMESTAMP = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;

/**
 * Epoch milliseconds the elapsed time counts from. `startedAt` may be epoch
 * ms, a Date or a date string from the API; a timestamp string without a
 * zone is read as UTC (as the backend writes and reads them). Missing
 * (`undefined`/`null`/empty) or unparseable values count from `fallback`
 * (the component's mount time).
 *
 * @param {number|Date|string|null|undefined} startedAt
 * @param {number} fallback
 * @returns {number}
 */
export function resolveStartTime(startedAt, fallback) {
  if (startedAt === undefined || startedAt === null || startedAt === '') return fallback;
  let value = startedAt;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    value = NAIVE_TIMESTAMP.test(trimmed) ? `${trimmed.replace(' ', 'T')}Z` : trimmed;
  }
  const time = new Date(value).getTime();
  return Number.isFinite(time) ? time : fallback;
}

/**
 * The elapsed label at `now` for a job that started at `startedAt`
 * (counting from `fallback` when there is no usable start).
 *
 * @param {number|Date|string|null|undefined} startedAt
 * @param {number} now
 * @param {number} fallback
 * @returns {string}
 */
export function elapsedSince(startedAt, now, fallback) {
  return formatElapsed(now - resolveStartTime(startedAt, fallback));
}
