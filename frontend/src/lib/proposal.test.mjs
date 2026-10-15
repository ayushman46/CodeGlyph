import assert from 'node:assert/strict';
import test from 'node:test';
import { approximatePaths, incompleteness, issueLabel } from './proposal.js';

test('issueLabel says Fixes only for a confirmed issue', () => {
  assert.equal(issueLabel({ issue_reference: 42, issue_confirmed: true }), 'Fixes #42');
  assert.equal(issueLabel({ issue_reference: 42, issue_confirmed: false }), 'Refs #42');
  assert.equal(issueLabel({ issue_reference: 42 }), 'Refs #42');
  assert.equal(issueLabel({ issue_reference: null }), '');
});

test('incompleteness and approximate files come from the proposal fields', () => {
  assert.deepEqual(incompleteness({}), { unchanged: [], incomplete: false });
  assert.equal(incompleteness({ incomplete: true }).incomplete, true);
  const result = incompleteness({ unchanged_files: [{ file_path: 'a.ts', reason: 'budget' }, null] });
  assert.equal(result.incomplete, true);
  assert.deepEqual(result.unchanged, [{ file_path: 'a.ts', reason: 'budget' }]);
  assert.deepEqual([...approximatePaths({ approximate_files: ['x.ts', ''] })], ['x.ts']);
  assert.equal(approximatePaths({}).size, 0);
});

test('branchNameFor cuts the title at a word boundary and keeps the prefix', async () => {
  const { branchNameFor, branchProblem } = await import('./proposal.js');
  const branch = branchNameFor('Rename extractBaseEmail to getBaseEmail in packages/lib/extract-base-email.ts', 'ab12c');
  assert.equal(branch, 'codebase-intel/rename-extractbaseemail-to-getbaseemail-ab12c');
  assert.equal(branchProblem(branch), '');
  assert.equal(branchNameFor('', 'x1'), 'codebase-intel/change-x1');
  assert.equal(branchNameFor('Fix it'), 'codebase-intel/fix-it');
});

test('branchProblem explains each invalid branch name', async () => {
  const { branchProblem } = await import('./proposal.js');
  assert.match(branchProblem('feature/x'), /must start with codebase-intel\//);
  assert.match(branchProblem('codebase-intel/'), /Add a name/);
  assert.match(branchProblem('codebase-intel/has space'), /no spaces/);
  for (const bad of ['codebase-intel/a..b', 'codebase-intel/a//b', 'codebase-intel/a/', 'codebase-intel/a.', 'codebase-intel/x.lock', 'codebase-intel/.hidden']) {
    assert.notEqual(branchProblem(bad), '', bad);
  }
  assert.equal(branchProblem('  codebase-intel/ok-1.2/x  '), '');
});

test('buildPrBody adds a testing checklist once and a footer', async () => {
  const { buildPrBody } = await import('./proposal.js');
  const body = buildPrBody({ pr_body: '## Summary\nDo it.', validation: ['yarn test', ''] });
  assert.match(body, /## Testing\n- \[ \] yarn test/);
  assert.match(body, /Generated with Codebase Intel/);
  assert.doesNotMatch(buildPrBody({ pr_body: '## Testing\nmanual', validation: ['x'] }), /- \[ \] x/);
  assert.match(buildPrBody({ summary: 'Short' }), /## Summary\n\nShort/);
});

test('truncationParts keeps the distinguishing end of long file names visible', async () => {
  const { truncationParts } = await import('./proposal.js');
  assert.deepEqual(truncationParts('index.ts'), { head: 'index.ts', tail: '' });
  const test = truncationParts('BookingAttendeesRemoveService.integration-test.ts');
  const plain = truncationParts('BookingAttendeesRemoveService.ts');
  assert.equal(test.head + test.tail, 'BookingAttendeesRemoveService.integration-test.ts');
  assert.equal(plain.head + plain.tail, 'BookingAttendeesRemoveService.ts');
  assert.equal(test.tail, '.integration-test.ts');
  assert.equal(plain.tail, 'RemoveService.ts');
  assert.notEqual(test.tail, plain.tail);
});

test('pull requests are remembered per proposal without a browser', async () => {
  const { pullRequestFor, rememberPullRequest, safeGithubUrl, subscribePullRequests } = await import('./proposal.js');
  let calls = 0;
  const stop = subscribePullRequests(() => { calls += 1; });
  assert.equal(pullRequestFor('p1'), null);
  rememberPullRequest('p1', { pr_url: 'https://github.com/calcom/cal.com/pull/9', pr_number: 9, branch_name: 'codebase-intel/x' }, 1);
  const first = pullRequestFor('p1');
  assert.equal(first.pr_number, 9);
  assert.equal(pullRequestFor('p1'), first, 'stable reference for useSyncExternalStore');
  rememberPullRequest('p2', { pr_url: 'javascript:alert(1)', pr_number: 1 });
  assert.equal(pullRequestFor('p2'), null);
  assert.equal(calls, 1);
  stop();
  assert.equal(safeGithubUrl('http://github.com/x'), '');
  assert.equal(safeGithubUrl('https://evil.example/github.com'), '');
});

test('a file requested from the chat card is read until cleared', async () => {
  const { clearReviewFile, peekReviewFile, requestReviewFile } = await import('./proposal.js');
  requestReviewFile('p1', 'src/a.ts');
  assert.equal(peekReviewFile('p1'), 'src/a.ts');
  assert.equal(peekReviewFile('p1'), 'src/a.ts');
  clearReviewFile('p1');
  assert.equal(peekReviewFile('p1'), '');
});

test('the diff layout preference falls back to unified without storage', async () => {
  const { readDiffLayout, saveDiffLayout } = await import('./proposal.js');
  saveDiffLayout('split');
  assert.equal(readDiffLayout(), 'unified');
});
