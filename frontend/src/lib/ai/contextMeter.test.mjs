import assert from 'node:assert/strict';
import test from 'node:test';
import {
  RING_CIRCUMFERENCE, barWidthFor, bytesLabels, clampUsed, defaultContextMeterLabels, filesLabels, meterRatio,
  meterStatus, remainingOf, resolveLabels, ringDashFor, statusLabelFor, toPercent,
} from './contextMeter.js';

test('toPercent rounds down below the limit, so 99.6% reads 99%', () => {
  assert.equal(toPercent(0.996), 99);
  assert.equal(toPercent(0.999999), 99);
  assert.equal(toPercent(meterRatio(199_999_999, 200_000_000)), 99);
  assert.equal(toPercent(0.425), 42);
  assert.equal(toPercent(0.009), 0);
  assert.equal(toPercent(0.01), 1);
});

test('toPercent shows 100% only when full', () => {
  assert.equal(toPercent(1), 100);
  assert.equal(toPercent(meterRatio(200, 200)), 100);
  for (let used = 0; used < 1000; used += 1) {
    assert.ok(toPercent(meterRatio(used, 1000)) < 100, `${used}/1000 must read below 100%`);
  }
  // Past the limit even by a sliver, the real figure (rounded) is shown.
  assert.equal(toPercent(1.004), 100);
});

test('toPercent shows the real figure past the limit', () => {
  assert.equal(toPercent(1.04), 104);
  assert.equal(toPercent(meterRatio(208, 200)), 104);
  assert.equal(toPercent(2.5), 250);
});

test('toPercent is not thrown off by binary rounding', () => {
  // 0.29 * 100 === 28.999999999999996 in IEEE 754.
  assert.equal(toPercent(meterRatio(29, 100)), 29);
  assert.equal(toPercent(meterRatio(57, 100)), 57);
  assert.equal(toPercent(meterRatio(58_000_000, 200_000_000)), 29);
  for (let used = 0; used <= 100; used += 1) {
    assert.equal(toPercent(meterRatio(used, 100)), used);
  }
});

test('toPercent reads 0 for no ratio', () => {
  assert.equal(toPercent(0), 0);
  assert.equal(toPercent(-0.5), 0);
  assert.equal(toPercent(Number.NaN), 0);
  assert.equal(toPercent(Number.POSITIVE_INFINITY), 0);
});

test('meterRatio is 0 when the limit is zero or negative', () => {
  assert.equal(meterRatio(50, 0), 0);
  assert.equal(meterRatio(50, -10), 0);
  assert.equal(meterRatio(50, Number.NaN), 0);
  assert.equal(toPercent(meterRatio(50, 0)), 0);
  assert.equal(meterStatus(meterRatio(50, 0)), 'ok');
});

test('meterRatio clamps used at zero and may exceed 1', () => {
  assert.equal(meterRatio(-20, 100), 0);
  assert.equal(meterRatio(50, 100), 0.5);
  assert.equal(meterRatio(150, 100), 1.5);
  assert.equal(clampUsed(-3), 0);
  assert.equal(clampUsed(undefined), 0);
  assert.equal(clampUsed('12'), 12);
});

test('meterStatus: ok below nearAt, near from nearAt, full from the limit', () => {
  assert.equal(meterStatus(0), 'ok');
  assert.equal(meterStatus(0.7999), 'ok');
  assert.equal(meterStatus(0.8), 'near');
  assert.equal(meterStatus(meterRatio(80, 100)), 'near');
  assert.equal(meterStatus(meterRatio(79, 100)), 'ok');
  assert.equal(meterStatus(0.9999), 'near');
  assert.equal(meterStatus(1), 'full');
  assert.equal(meterStatus(1.04), 'full');
});

test('meterStatus honours custom nearAt boundaries', () => {
  assert.equal(meterStatus(0.49, 0.5), 'ok');
  assert.equal(meterStatus(0.5, 0.5), 'near');
  assert.equal(meterStatus(meterRatio(7, 10), 0.7), 'near');
  assert.equal(meterStatus(meterRatio(9_000, 10_000), 0.9), 'near');
  assert.equal(meterStatus(meterRatio(8_999, 10_000), 0.9), 'ok');
  // nearAt at (or past) the limit: never near, straight from ok to full.
  assert.equal(meterStatus(0.99, 1), 'ok');
  assert.equal(meterStatus(1, 1), 'full');
  assert.equal(meterStatus(0.99, 1.5), 'ok');
  assert.equal(meterStatus(1.2, 1.5), 'full');
  // nearAt 0: near from the start.
  assert.equal(meterStatus(0, 0), 'near');
});

test('statusLabelFor names near and full only', () => {
  assert.equal(statusLabelFor('ok'), null);
  assert.equal(statusLabelFor('near'), 'Near the limit');
  assert.equal(statusLabelFor('full'), 'At the limit');
  assert.equal(statusLabelFor('full', { ...defaultContextMeterLabels, full: 'Quota reached' }), 'Quota reached');
});

test('remainingOf never goes below zero', () => {
  assert.equal(remainingOf(30, 100), 70);
  assert.equal(remainingOf(130, 100), 0);
  assert.equal(remainingOf(-5, 100), 100);
  assert.equal(remainingOf(5, 0), 0);
});

test('ring and bar are capped full past the limit', () => {
  assert.equal(ringDashFor(0), `0 ${RING_CIRCUMFERENCE}`);
  assert.equal(ringDashFor(0.5), `${0.5 * RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`);
  assert.equal(ringDashFor(1.04), `${RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`);
  assert.equal(barWidthFor(0.42), '42%');
  assert.equal(barWidthFor(1.5), '100%');
  assert.equal(barWidthFor(0), '0%');
});

test('default labels match the reference strings', () => {
  const labels = defaultContextMeterLabels;
  assert.equal(labels.title, 'Context window');
  assert.equal(labels.summary(42), '42% of context used');
  assert.equal(labels.tokens(labels.number(84000), labels.number(200000)), '84,000 of 200,000 tokens');
  assert.equal(labels.remaining(labels.number(116000)), '116,000 left');
  assert.equal(labels.near, 'Near the limit');
  assert.equal(labels.full, 'At the limit');
  assert.equal(labels.cost, 'Cost');
});

test('resolveLabels merges a partial labels object over the defaults', () => {
  const labels = resolveLabels({ title: 'Session' });
  assert.equal(labels.title, 'Session');
  assert.equal(labels.summary(10), '10% of context used');
  assert.equal(resolveLabels(undefined).title, 'Context window');
});

test('bytesLabels formats every count as bytes', () => {
  const labels = bytesLabels;
  assert.equal(labels.title, 'Indexed source');
  assert.equal(labels.summary(11), '11% of indexed source used');
  assert.equal(labels.tokens(labels.number(22_000_000), labels.number(200_000_000)), '22 MB of 200 MB');
  assert.equal(labels.remaining(labels.number(remainingOf(22_000_000, 200_000_000))), '178 MB left');
  assert.equal(labels.number(1_536), '1.5 KB');
  // One shared precision, so the figures add up (171.4 + 28.6 = 200).
  assert.equal(labels.tokens(labels.number(171_400_000), labels.number(200_000_000)), '171.4 MB of 200 MB');
  assert.equal(labels.remaining(labels.number(remainingOf(171_400_000, 200_000_000))), '28.6 MB left');
  assert.equal(labels.number(999), '999 B');
  assert.equal(labels.number(999_960), '1 MB');
  assert.equal(labels.number(1_250_000_000), '1.3 GB');
  assert.equal(labels.near, 'Near the limit');
  assert.equal(labels.full, 'At the limit');
});

test('filesLabels counts files', () => {
  const labels = filesLabels;
  assert.equal(labels.title, 'Files indexed');
  assert.equal(labels.summary(94), '94% of the file budget used');
  assert.equal(labels.tokens(labels.number(9_412), labels.number(10_000)), '9,412 of 10,000 files');
  assert.equal(labels.remaining(labels.number(588)), '588 left');
});
