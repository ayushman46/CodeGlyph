import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DEFAULT_LOADING_PATTERN,
  LIT_CELL_OPACITY,
  LOADING_CELL_ANIMATION,
  LOADING_PATTERN_NAMES,
  LOADING_PATTERNS,
  LOADING_PULSE_ANIMATION,
  PERIMETER,
  UNLIT_CELL_OPACITY,
  elapsedSince,
  formatElapsed,
  loadingCellStyle,
  resolveLoadingPattern,
  resolveStartTime,
} from './loadingState.js';

test('formatElapsed shows seconds under a minute, then minutes and seconds', () => {
  assert.equal(formatElapsed(0), '0s');
  assert.equal(formatElapsed(999), '0s');
  assert.equal(formatElapsed(1000), '1s');
  assert.equal(formatElapsed(42_000), '42s');
  assert.equal(formatElapsed(42_999), '42s');
  assert.equal(formatElapsed(59_999), '59s');
  assert.equal(formatElapsed(60_000), '1m 0s');
  assert.equal(formatElapsed(72_000), '1m 12s');
  assert.equal(formatElapsed(3_599_000), '59m 59s');
  // Minutes keep counting past an hour, as in the reference.
  assert.equal(formatElapsed(3_600_000), '60m 0s');
  assert.equal(formatElapsed(4_503_000), '75m 3s');
});

test('formatElapsed never goes negative and survives non-numbers', () => {
  assert.equal(formatElapsed(-1), '0s');
  assert.equal(formatElapsed(-90_000), '0s');
  assert.equal(formatElapsed(Number.NaN), '0s');
  assert.equal(formatElapsed(Number.POSITIVE_INFINITY), '0s');
  assert.equal(formatElapsed(undefined), '0s');
});

test('every pattern has the documented cell count, duration and look', () => {
  assert.deepEqual([...LOADING_PATTERN_NAMES], ['drive', 'dots', 'orbit', 'diagonal', 'diamond', 'square', 'pulse']);
  const shape = Object.fromEntries(LOADING_PATTERN_NAMES.map((name) => {
    const { delays, durationMs, isRound, hideEmpty, animationName } = resolveLoadingPattern(name);
    return [name, { cells: delays.length, durationMs, isRound, hideEmpty, animationName }];
  }));
  assert.deepEqual(shape, {
    drive: { cells: 9, durationMs: 650, isRound: false, hideEmpty: false, animationName: LOADING_CELL_ANIMATION },
    dots: { cells: 9, durationMs: 650, isRound: true, hideEmpty: false, animationName: LOADING_CELL_ANIMATION },
    orbit: { cells: 9, durationMs: 950, isRound: false, hideEmpty: false, animationName: LOADING_CELL_ANIMATION },
    diagonal: { cells: 9, durationMs: 1200, isRound: false, hideEmpty: false, animationName: LOADING_CELL_ANIMATION },
    diamond: { cells: 9, durationMs: 1200, isRound: false, hideEmpty: true, animationName: LOADING_CELL_ANIMATION },
    square: { cells: 9, durationMs: 1200, isRound: false, hideEmpty: true, animationName: LOADING_CELL_ANIMATION },
    pulse: { cells: 3, durationMs: 1200, isRound: false, hideEmpty: false, animationName: LOADING_PULSE_ANIMATION },
  });
});

test('drive and dots share the wave out from the middle of the left column', () => {
  // (column + |row - 1|) * 90
  assert.deepEqual([...LOADING_PATTERNS.drive.delays], [90, 180, 270, 0, 90, 180, 90, 180, 270]);
  assert.equal(LOADING_PATTERNS.dots.delays, LOADING_PATTERNS.drive.delays);
});

test('orbit runs clockwise around the edge with an unlit center', () => {
  assert.deepEqual([...PERIMETER], [0, 1, 2, 5, 8, 7, 6, 3]);
  assert.deepEqual([...LOADING_PATTERNS.orbit.delays], [0, 110, 220, 770, null, 330, 660, 550, 440]);
});

test('diagonal sweeps from the top-left corner to the bottom-right', () => {
  assert.deepEqual([...LOADING_PATTERNS.diagonal.delays], [-1200, -1050, -900, -1050, -900, -750, -900, -750, -600]);
});

test('diamond lights top, right, bottom, left and the center only', () => {
  assert.deepEqual([...LOADING_PATTERNS.diamond.delays], [null, -1200, null, -300, -750, -900, null, -600, null]);
  const lit = LOADING_PATTERNS.diamond.delays.flatMap((delay, index) => (delay === null ? [] : [index]));
  assert.deepEqual(lit, [1, 3, 4, 5, 7]);
});

test('square lights the eight edge cells around an empty center', () => {
  assert.deepEqual([...LOADING_PATTERNS.square.delays], [-1200, -1050, -900, -150, null, -750, -300, -450, -600]);
});

test('pulse is three cells in sequence on the pulse keyframes', () => {
  assert.deepEqual([...LOADING_PATTERNS.pulse.delays], [-1200, -1000, -800]);
  assert.equal(resolveLoadingPattern('pulse').animationName, 'ci-loading-pulse');
});

test('the pattern tables cannot be mutated by a consumer', () => {
  assert.ok(Object.isFrozen(LOADING_PATTERNS));
  assert.ok(Object.isFrozen(LOADING_PATTERNS.drive));
  assert.ok(Object.isFrozen(LOADING_PATTERNS.drive.delays));
  assert.throws(() => { LOADING_PATTERNS.drive.delays[0] = 5; }, TypeError);
});

test('resolveLoadingPattern defaults to drive and falls back to it for unknown names', () => {
  assert.equal(DEFAULT_LOADING_PATTERN, 'drive');
  assert.equal(resolveLoadingPattern().name, 'drive');
  assert.equal(resolveLoadingPattern('spinner').name, 'drive');
  assert.equal(resolveLoadingPattern('toString').name, 'drive');
  assert.equal(resolveLoadingPattern('square').name, 'square');
});

test('loadingCellStyle animates lit cells from their resting opacity', () => {
  assert.equal(LIT_CELL_OPACITY, 0.15);
  assert.equal(UNLIT_CELL_OPACITY, 0.08);
  assert.deepEqual(loadingCellStyle(90, resolveLoadingPattern('drive')), {
    opacity: 0.15,
    visibility: undefined,
    animation: 'ci-loading-cell 650ms ease-in-out 90ms infinite',
  });
  assert.deepEqual(loadingCellStyle(-1000, resolveLoadingPattern('pulse')), {
    opacity: 0.15,
    visibility: undefined,
    animation: 'ci-loading-pulse 1200ms ease-in-out -1000ms infinite',
  });
  // A zero delay is still a lit cell.
  assert.equal(loadingCellStyle(0, resolveLoadingPattern('orbit')).animation, 'ci-loading-cell 950ms ease-in-out 0ms infinite');
});

test('loadingCellStyle draws unlit cells very dim, or hides them when the pattern says so', () => {
  assert.deepEqual(loadingCellStyle(null, resolveLoadingPattern('orbit')), { opacity: 0.08, visibility: undefined, animation: undefined });
  assert.deepEqual(loadingCellStyle(null, resolveLoadingPattern('diamond')), { opacity: 0.08, visibility: 'hidden', animation: undefined });
  assert.deepEqual(loadingCellStyle(null, resolveLoadingPattern('square')), { opacity: 0.08, visibility: 'hidden', animation: undefined });
  assert.equal(loadingCellStyle(null, { durationMs: 650 }).animation, undefined);
});

test('resolveStartTime accepts epoch ms, Dates and zoned ISO strings', () => {
  const at = Date.UTC(2026, 9, 20, 12, 0, 0);
  assert.equal(resolveStartTime(at, 1), at);
  assert.equal(resolveStartTime(new Date(at), 1), at);
  assert.equal(resolveStartTime('2026-10-20T12:00:00Z', 1), at);
  assert.equal(resolveStartTime('2026-10-20T12:00:00+00:00', 1), at);
  assert.equal(resolveStartTime('2026-10-20T14:00:00+02:00', 1), at);
  // Python isoformat() with microseconds.
  assert.equal(resolveStartTime('2026-10-20T12:00:00.123456+00:00', 1), at + 123);
  assert.equal(resolveStartTime(0, 1), 0);
});

test('resolveStartTime reads a timestamp without a zone as UTC', () => {
  const at = Date.UTC(2026, 9, 20, 12, 0, 0);
  assert.equal(resolveStartTime('2026-10-20T12:00:00', 1), at);
  assert.equal(resolveStartTime('2026-10-20 12:00:00', 1), at);
  assert.equal(resolveStartTime(' 2026-10-20 12:00:00.5 ', 1), at + 500);
  assert.equal(resolveStartTime('2026-10-20T12:00', 1), at);
});

test('resolveStartTime counts from the fallback when there is no usable start', () => {
  assert.equal(resolveStartTime(undefined, 42), 42);
  assert.equal(resolveStartTime(null, 42), 42);
  assert.equal(resolveStartTime('', 42), 42);
  assert.equal(resolveStartTime('not a date', 42), 42);
  assert.equal(resolveStartTime(new Date(Number.NaN), 42), 42);
  assert.equal(resolveStartTime(Number.NaN, 42), 42);
});

test('elapsedSince formats the time from the start (or the fallback) to now', () => {
  const now = Date.UTC(2026, 9, 20, 12, 1, 12);
  assert.equal(elapsedSince(now - 42_000, now, now), '42s');
  assert.equal(elapsedSince('2026-10-20T12:00:00Z', now, now), '1m 12s');
  assert.equal(elapsedSince(undefined, now, now - 5_000), '5s');
  // A start in the future (clock skew between server and browser).
  assert.equal(elapsedSince(now + 30_000, now, now), '0s');
});
