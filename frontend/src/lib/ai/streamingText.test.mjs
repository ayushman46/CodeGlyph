import assert from 'node:assert/strict';
import test from 'node:test';
import {
  COPIED_MILLISECONDS, DEFAULT_CHARACTERS_PER_SECOND, IDLE_REVEAL_CLOCK, MAX_FRAME_CARRY_MS, PACE_MAX_CHARACTERS, PACE_MAX_MESSAGES,
  advanceReveal, citationsToSources, containsCode, defaultStreamingTextLabels, frameElapsed, isPlainClick,
  nextFeedback, resolveLabels, revealFrame, shouldPaceReply, sourceKeys, visibleLength,
} from './streamingText.js';

test('defaults match the reference contract', () => {
  assert.equal(DEFAULT_CHARACTERS_PER_SECOND, 110);
  assert.equal(COPIED_MILLISECONDS, 1600);
  assert.equal(defaultStreamingTextLabels.copy, 'Copy reply');
  assert.equal(defaultStreamingTextLabels.copied, 'Copied');
  assert.equal(defaultStreamingTextLabels.copyFailed, 'Copy failed');
  assert.equal(defaultStreamingTextLabels.retry, 'Try again');
  assert.equal(defaultStreamingTextLabels.helpful, 'Helpful');
  assert.equal(defaultStreamingTextLabels.notHelpful, 'Not helpful');
  assert.equal(defaultStreamingTextLabels.error, 'This reply stopped before it finished.');
  assert.equal(defaultStreamingTextLabels.sources(1), '1 source');
  assert.equal(defaultStreamingTextLabels.sources(0), '0 sources');
  assert.equal(defaultStreamingTextLabels.sources(4), '4 sources');
});

test('resolveLabels merges over the defaults and ignores undefined keys', () => {
  const labels = resolveLabels({ copy: 'Copy answer', retry: undefined, sources: (n) => `${n} files` });
  assert.equal(labels.copy, 'Copy answer');
  assert.equal(labels.retry, 'Try again');
  assert.equal(labels.sources(2), '2 files');
  assert.equal(labels.error, defaultStreamingTextLabels.error);
  assert.deepEqual(Object.keys(resolveLabels(undefined)).sort(), Object.keys(defaultStreamingTextLabels).sort());
  // The defaults object itself is never mutated.
  assert.equal(defaultStreamingTextLabels.copy, 'Copy reply');
});

test('visibleLength never runs ahead of the received text', () => {
  assert.equal(visibleLength(0, 10), 0);
  assert.equal(visibleLength(4, 10), 4);
  assert.equal(visibleLength(12, 10), 10);
  assert.equal(visibleLength(-3, 10), 0);
});

test('advanceReveal moves at the given rate and stops at the received length', () => {
  assert.equal(advanceReveal(0, 1000, 110, 500), 110);
  assert.equal(advanceReveal(10, 500, 100, 500), 60);
  assert.equal(advanceReveal(490, 1000, 110, 500), 500);
  assert.equal(advanceReveal(20, 0, 110, 500), 20);
  assert.equal(advanceReveal(20, -16, 110, 500), 20);
  // Bad rates fall back to the default instead of producing NaN.
  assert.equal(advanceReveal(0, 1000, Number.NaN, 500), DEFAULT_CHARACTERS_PER_SECOND);
  assert.equal(advanceReveal(0, 1000, 0, 500), DEFAULT_CHARACTERS_PER_SECOND);
});

test('frameElapsed: first frame of a fresh reveal is 0; a quick re-run carries over', () => {
  assert.equal(frameElapsed(undefined, 100), 0);
  assert.equal(frameElapsed(100, 116.6), 16.599999999999994);
  assert.equal(frameElapsed(100, 100 + MAX_FRAME_CARRY_MS, { firstOfRun: true }), MAX_FRAME_CARRY_MS);
  assert.equal(frameElapsed(100, 101 + MAX_FRAME_CARRY_MS, { firstOfRun: true }), 0);
  // Within a run the gap is not capped (a hidden tab catches up on return).
  assert.equal(frameElapsed(100, 5100), 5000);
  assert.equal(frameElapsed(100, 90), 0);
});

/**
 * Simulates the hook's loop with the same revealFrame the hook calls: frames
 * every 16ms, a chunk of `chunk` characters every `every` ms until `total`,
 * the loop restarting from the visible length on every chunk (as the effect
 * does when `text` changes).
 */
const simulate = ({ total, chunk, every, cps = 110, until }) => {
  let received = 0;
  let shown = 0;
  let position = 0;
  let clock = IDLE_REVEAL_CLOCK;
  let running = false;
  let firstOfRun = true;
  const samples = [];
  for (let time = 0; time <= until; time += 1) {
    if (time % every === 0 && received < total) {
      received = Math.min(total, received + chunk);
      position = shown;
      running = shown < received;
      firstOfRun = true;
    }
    if (running && time % 16 === 0) {
      const next = revealFrame({ position, firstOfRun, clock, time, length: received, charactersPerSecond: cps });
      firstOfRun = false;
      position = next.position;
      clock = next.clock;
      shown = next.shown;
      assert.ok(shown <= received, `ran ahead at ${time}ms: ${shown} > ${received}`);
      if (next.done) running = false;
    }
    samples.push({ time, shown, received });
  }
  return samples;
};

test('the reveal keeps a steady rate while chunks arrive every 40 ms', () => {
  const samples = simulate({ total: 2000, chunk: 12, every: 40, until: 4000 });
  const at = (time) => samples[time].shown;
  // 110 characters per second, within a frame either side.
  assert.ok(Math.abs(at(2000) - 220) <= 4, `shown at 2s: ${at(2000)}`);
  assert.ok(Math.abs(at(4000) - 440) <= 4, `shown at 4s: ${at(4000)}`);
});

test('revealFrame resets the clock when the reveal reaches the received text', () => {
  const first = revealFrame({ position: 0, firstOfRun: true, clock: IDLE_REVEAL_CLOCK, time: 0, length: 5, charactersPerSecond: 110 });
  assert.deepEqual(first, { position: 0, shown: 0, done: false, clock: { time: 0, fraction: 0 } });
  const second = revealFrame({ position: 0, firstOfRun: false, clock: first.clock, time: 20, length: 5, charactersPerSecond: 110 });
  assert.equal(second.shown, 2);
  assert.ok(Math.abs(second.clock.fraction - 0.2) < 1e-9);
  const last = revealFrame({ position: 2, firstOfRun: true, clock: second.clock, time: 60, length: 5, charactersPerSecond: 110 });
  assert.equal(last.done, true);
  assert.equal(last.shown, 5);
  assert.equal(last.clock, IDLE_REVEAL_CLOCK);
  // A stale clock (the previous frame long ago) starts the run fresh.
  const stale = revealFrame({ position: 1, firstOfRun: true, clock: { time: 0, fraction: 0.9 }, time: 1000, length: 5, charactersPerSecond: 110 });
  assert.equal(stale.position, 1);
});

test('the reveal catches up when the stream ends and then settles', () => {
  const samples = simulate({ total: 120, chunk: 120, every: 10000, until: 1500 });
  assert.equal(samples[0].received, 120);
  assert.ok(samples[500].shown > 40 && samples[500].shown < 70, `at 0.5s: ${samples[500].shown}`);
  assert.equal(samples[1500].shown, 120);
});

test('nextFeedback toggles and clears', () => {
  assert.equal(nextFeedback(null, 'up'), 'up');
  assert.equal(nextFeedback('up', 'up'), null);
  assert.equal(nextFeedback('up', 'down'), 'down');
  assert.equal(nextFeedback('down', 'down'), null);
});

test('containsCode spots fenced blocks only', () => {
  assert.equal(containsCode('Use `ky.get()` here.'), false);
  assert.equal(containsCode('Example:\n```js\nky.get(url)\n```'), true);
  assert.equal(containsCode('```\nstill streaming'), true);
  assert.equal(containsCode('Example:\n  ~~~\ncode\n  ~~~'), true);
  assert.equal(containsCode('text with ``` in the middle'), false);
  assert.equal(containsCode(undefined), false);
});

test('shouldPaceReply: short, simple replies in short conversations only', () => {
  const short = 'Retries are configured in `source/core/constants.ts:L12–30`.';
  assert.equal(shouldPaceReply({ text: short, messageCount: 2 }), true);
  assert.equal(shouldPaceReply({ text: '', messageCount: 2 }), true, 'a reply that has not started yet is paced');
  assert.equal(shouldPaceReply({ text: 'x'.repeat(PACE_MAX_CHARACTERS), messageCount: 2 }), true);
  assert.equal(shouldPaceReply({ text: 'x'.repeat(PACE_MAX_CHARACTERS + 1), messageCount: 2 }), false);
  assert.equal(shouldPaceReply({ text: short, messageCount: PACE_MAX_MESSAGES }), true);
  assert.equal(shouldPaceReply({ text: short, messageCount: PACE_MAX_MESSAGES + 1 }), false);
  assert.equal(shouldPaceReply({ text: short, messageCount: 2, hasCode: true }), false);
  assert.equal(shouldPaceReply({ text: 'Like this:\n```ts\nky(url)\n```', messageCount: 2 }), false);
  // An explicit hasCode: false trusts the caller.
  assert.equal(shouldPaceReply({ text: 'Like this:\n```ts\nky(url)\n```', messageCount: 2, hasCode: false }), true);
  assert.equal(shouldPaceReply({ text: short }), true);
  assert.equal(shouldPaceReply({ text: short, messageCount: Number.NaN }), true);
  assert.equal(shouldPaceReply(), true);
  assert.equal(shouldPaceReply({ text: null, messageCount: 1 }), true);
  assert.equal(PACE_MAX_CHARACTERS, 1200);
  assert.equal(PACE_MAX_MESSAGES, 6);
});

test('shouldPaceReply only switches from pacing to not pacing as a reply grows', () => {
  let text = '';
  let wasPaced = true;
  let switches = 0;
  for (let index = 0; index < 400; index += 1) {
    text += index === 200 ? '\n```js\n' : 'word ';
    const paced = shouldPaceReply({ text, messageCount: 4 });
    if (paced !== wasPaced) {
      switches += 1;
      assert.equal(paced, false);
    }
    wasPaced = paced;
  }
  assert.equal(switches, 1);
});

test('isPlainClick keeps modified and middle clicks for the link', () => {
  assert.equal(isPlainClick({ button: 0 }), true);
  assert.equal(isPlainClick({}), true);
  assert.equal(isPlainClick({ button: 1 }), false);
  assert.equal(isPlainClick({ button: 0, metaKey: true }), false);
  assert.equal(isPlainClick({ button: 0, ctrlKey: true }), false);
  assert.equal(isPlainClick({ button: 0, shiftKey: true }), false);
  assert.equal(isPlainClick({ button: 0, altKey: true }), false);
  assert.equal(isPlainClick({ button: 0, defaultPrevented: true }), false);
  assert.equal(isPlainClick(null), false);
});

test('sourceKeys are unique and prefer id, then href, then title', () => {
  assert.deepEqual(
    sourceKeys([{ id: 'a', href: 'h' }, { href: 'h' }, { title: 't' }, { title: 't' }, {}, { href: 'h' }]),
    ['a', 'h', 't', 't#1', 'source-4', 'h#1'],
  );
  assert.deepEqual(sourceKeys(undefined), []);
});

test('citationsToSources lists cited sections first and opens them in the app', () => {
  const citations = [
    { file_path: 'source/core/Ky.ts', start_line: 40, end_line: 88, cited_in_answer: false },
    { file_path: 'source/utils/merge.ts', start_line: 7, end_line: 7, cited_in_answer: true },
    { file_path: 'readme.md', cited_in_answer: true },
  ];
  const opened = [];
  const sources = citationsToSources(citations, {
    onSelect: (citation, event) => opened.push([citation.file_path, event]),
    hrefFor: (citation) => (citation.start_line ? `https://github.com/o/r/blob/sha/${citation.file_path}#L${citation.start_line}` : null),
  });
  assert.deepEqual(sources.map((source) => source.title), ['source/utils/merge.ts:L7', 'readme.md', 'source/core/Ky.ts:L40–88']);
  assert.deepEqual(sources.map((source) => source.domain), ['cited', 'cited', 'retrieved']);
  assert.equal(sources[0].href, 'https://github.com/o/r/blob/sha/source/utils/merge.ts#L7');
  assert.equal(sources[1].href, undefined);
  assert.equal(new Set(sources.map((source) => source.id)).size, 3);
  sources[2].onSelect('click');
  assert.deepEqual(opened, [['source/core/Ky.ts', 'click']]);
  // Unmarked citations (older servers) all count as cited.
  assert.deepEqual(citationsToSources([{ file_path: 'a.ts' }]).map((source) => [source.domain, source.onSelect]), [['cited', undefined]]);
  assert.deepEqual(citationsToSources(undefined), []);
});
