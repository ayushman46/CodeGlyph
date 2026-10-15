import assert from 'node:assert/strict';
import test from 'node:test';
import { formatBytes, formatCount, formatDuration, relativeTime, shownLabel } from './format.js';

test('formatBytes moves to the next unit after rounding (FE-11)', () => {
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(512.4), '512 B');
  assert.equal(formatBytes(999), '999 B');
  assert.equal(formatBytes(1000), '1 KB');
  assert.equal(formatBytes(1536), '1.5 KB');
  assert.equal(formatBytes(999_499), '999 KB');
  assert.equal(formatBytes(999_950), '1 MB');
  assert.equal(formatBytes(999_999_999), '1 GB');
  assert.equal(formatBytes(12_345_678), '12.3 MB');
  assert.equal(formatBytes(-5), '0 B');
});

test('formatCount never shows 1000k (FE-11)', () => {
  assert.equal(formatCount(9999), (9999).toLocaleString());
  assert.equal(formatCount(12_345), '12.3k');
  assert.equal(formatCount(99_960), '100k');
  assert.equal(formatCount(999_499), '999k');
  assert.equal(formatCount(999_999), '1M');
  assert.equal(formatCount(2_500_000), '2.5M');
});

test('formatDuration rounds before choosing the precision (FE-11)', () => {
  assert.equal(formatDuration(999.6), '1.0 s');
  assert.equal(formatDuration(999.4), '999 ms');
  assert.equal(formatDuration(1234), '1.2 s');
  assert.equal(formatDuration(9949), '9.9 s');
  assert.equal(formatDuration(9960), '10 s');
  assert.equal(formatDuration(12_600), '13 s');
  assert.equal(formatDuration(59_600), '1 min');
  assert.equal(formatDuration(999_960), '16 min 40 s');
  assert.equal(formatDuration(3_600_000), '1 h');
  assert.equal(formatDuration(5_400_000), '1 h 30 min');
});

test('relativeTime handles past and future instants', () => {
  assert.equal(relativeTime(new Date(Date.now() - 10_000).toISOString()), 'just now');
  assert.equal(relativeTime(new Date(Date.now() + 2 * 86_400_000).toISOString()), new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(2, 'day'));
  assert.equal(relativeTime('not a date'), '');
});

test('shownLabel says when a list is only a sample', () => {
  assert.equal(shownLabel(200, 3400), `200 of ${(3400).toLocaleString()} shown`);
  assert.equal(shownLabel(12, 12), '12 shown');
  assert.equal(shownLabel(12), '12 shown');
});

test('parseRepositoryInput accepts every form the server accepts and explains the rest', async () => {
  const { parseRepositoryInput, REPOSITORY_INPUT_MESSAGES: M } = await import('./format.js');
  for (const value of [
    'calcom/cal.com', ' calcom/cal.com ', 'https://github.com/calcom/cal.com', 'http://github.com/calcom/cal.com/',
    'github.com/calcom/cal.com', 'www.github.com/calcom/cal.com.git', 'git@github.com:calcom/cal.com.git',
    'https://github.com/calcom/cal.com?tab=readme#top', 'https://github.com/calcom/cal.com/issues',
  ]) {
    const parsed = parseRepositoryInput(value);
    assert.equal(parsed.ok, true, value);
    assert.equal(parsed.fullName, 'calcom/cal.com', value);
    assert.equal(parsed.ref, null, value);
  }
  assert.deepEqual(parseRepositoryInput('https://github.com/calcom/cal.com/tree/main/apps').ref, { kind: 'ref', label: 'main/apps' });
  assert.deepEqual(parseRepositoryInput('github.com/calcom/cal.com/commit/54343aa685ae8f33').ref, { kind: 'commit', label: '54343aa' });
  assert.deepEqual(parseRepositoryInput(''), { ok: false, reason: 'empty', error: '' });
  assert.equal(parseRepositoryInput('not a repo').error, M.characters);
  assert.equal(parseRepositoryInput('calcom').error, M.shape);
  assert.equal(parseRepositoryInput('https://gitlab.com/a/b').error, M.host);
  assert.equal(parseRepositoryInput('gitlab.com/a/b').error, M.host);
  assert.equal(parseRepositoryInput('ftp://github.com/a/b').error, M.scheme);
  assert.equal(parseRepositoryInput('https://user:token@github.com/a/b').error, M.credentials);
  assert.equal(parseRepositoryInput('https://github.com:8443/a/b').error, M.port);
  assert.equal(parseRepositoryInput('-bad/repo').error, M.owner);
  assert.equal(parseRepositoryInput('https://github.com/settings/profile').error, M.reserved);
  assert.equal(parseRepositoryInput('calcom/..').error, M.repository);
  assert.equal(parseRepositoryInput('https://github.com/calcom/cal.com/somewhere').error, M.route);
});
