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

// ------------------------------------------------------------ quality gates (G6d, G10)

const check = (id, status, extra = {}) => ({ id, label: id[0].toUpperCase() + id.slice(1), status, detail: `${id} ${status}`, files: [], ...extra });

const withQuality = (quality, files = []) => ({ id: 'p', title: 'T', files, quality });

test('qualitySummary: a passing proposal', async () => {
  const { qualitySummary, qualityNote } = await import('./proposal.js');
  const summary = qualitySummary(withQuality({
    status: 'pass', reviewed: true, review_summary: 'No issues found.',
    checks: [check('syntax', 'pass'), check('imports', 'pass'), check('review', 'pass')],
    review_issues: [], autofixed: [{ file_path: 'a.ts', rule: 'final_newline', lines: 1 }],
  }));
  assert.equal(summary.status, 'pass');
  assert.deepEqual(summary.failing, []);
  assert.deepEqual(summary.warnings, []);
  assert.deepEqual(summary.passed.map((item) => item.id), ['syntax', 'imports', 'review']);
  assert.equal(summary.reviewed, true);
  assert.equal(summary.reviewSummary, 'No issues found.');
  assert.deepEqual(summary.autofixed, [{ file_path: 'a.ts', rule: 'final_newline', lines: 1 }]);
  assert.equal(qualityNote(summary), 'checks passed');
});

test('qualitySummary: warnings, and a skipped self-review counts as one', async () => {
  const { qualitySummary, qualityNote } = await import('./proposal.js');
  const summary = qualitySummary(withQuality({
    status: 'warn', reviewed: true, review_summary: 'Fixed the docs.',
    checks: [
      check('syntax', 'pass'),
      check('completeness', 'warn', { files: [{ file_path: 'readme.md', line: 264, column: null, message: 'still says `limit`: `2`' }] }),
      check('tests', 'warn'),
    ],
    review_issues: [{ severity: 'high', file_path: 'test/is.ts', line: 43, problem: 'wrong assertion', fixed: true }, { severity: 'bogus', problem: 'x', fixed: false }, { problem: '' }],
  }));
  assert.equal(summary.status, 'warn');
  assert.deepEqual(summary.warnings.map((item) => item.id), ['completeness', 'tests']);
  assert.deepEqual(summary.warnings[0].files, [{ file_path: 'readme.md', line: 264, column: null, message: 'still says `limit`: `2`' }]);
  assert.equal(summary.warnings[0].label, 'Completeness');
  assert.deepEqual(summary.reviewIssues.map((issue) => [issue.severity, issue.fixed]), [['high', true], ['medium', false]]);
  assert.equal(qualityNote(summary), '2 warnings');

  // The server says pass but the self-review was skipped: at least a warning.
  const skipped = qualitySummary(withQuality({
    status: 'pass', reviewed: false, review_summary: 'Self-review skipped: the model was unavailable',
    checks: [check('syntax', 'pass'), check('review', 'skipped', { label: '', detail: 'the model was unavailable' }), check('style', 'skipped')],
  }));
  assert.equal(skipped.status, 'warn');
  assert.deepEqual(skipped.warnings.map((item) => item.id), ['review']);
  assert.equal(skipped.warnings[0].label, 'Self-review');
  assert.deepEqual(skipped.skipped.map((item) => item.id), ['style']);
  assert.equal(skipped.reviewed, false);
  assert.equal(skipped.reviewSummary, 'Self-review skipped: the model was unavailable');
  assert.equal(qualityNote(skipped), '1 warning');
});

test('qualitySummary: failures win over the server status, and findings are capped and cleaned', async () => {
  const { qualitySummary, qualityNote } = await import('./proposal.js');
  const many = Array.from({ length: 30 }, (_, index) => ({ file_path: 'a.ts', line: index + 1, column: 0, message: 'm' }));
  const summary = qualitySummary(withQuality({
    status: 'warn', reviewed: true,
    checks: [check('syntax', 'fail', { files: [...many, null, 'x'] }), check('imports', 'fail'), check('tests', 'warn'), { status: 'fail' }, null],
  }));
  assert.equal(summary.status, 'fail');
  assert.deepEqual(summary.failing.map((item) => item.id), ['syntax', 'imports']);
  assert.equal(summary.failing[0].files.length, 20);
  assert.equal(summary.failing[0].files[0].column, null, 'column 0 is not a position');
  assert.equal(qualityNote(summary), 'syntax errors');
  assert.equal(qualityNote(qualitySummary(withQuality({ status: 'fail', checks: [check('imports', 'fail')] }))), 'import errors');
  assert.equal(qualityNote(qualitySummary(withQuality({ status: 'fail', checks: [check('scope', 'fail')] }))), '1 failed check');
  // An unknown check status is reported as not run, never as a pass or a failure.
  assert.equal(qualitySummary(withQuality({ checks: [check('syntax', 'exploded')] })).skipped[0].id, 'syntax');
});

test('qualitySummary: a review skipped on purpose (small change, every check passed) is not a warning', async () => {
  const { qualitySummary, qualityNote } = await import('./proposal.js');
  const summary = qualitySummary(withQuality({
    status: 'pass', reviewed: false,
    review_summary: 'Self-review skipped: the change is one small hunk in one file and every automatic check passed',
    checks: [check('syntax', 'pass'), check('review', 'skipped', { intentional: true })],
  }));
  assert.equal(summary.status, 'pass');
  assert.deepEqual(summary.warnings, []);
  assert.equal(summary.reviewSkippedOnPurpose, true);
  assert.equal(qualityNote(summary), 'checks passed');
});

test('qualitySummary: unverified review suggestions are kept apart and never warn a file', async () => {
  const { qualitySummary, fileQuality, CHECK_LABELS } = await import('./proposal.js');
  const proposal = withQuality({
    status: 'pass', reviewed: true,
    checks: [check('review', 'pass')],
    review_issues: [
      { severity: 'high', file_path: 'a.ts', line: 3, problem: 'null passes the check', fixed: false, verified: false,
        note: 'not applied: the review neither cited an automatic finding nor quoted the wrong code' },
      { severity: 'medium', file_path: 'b.ts', line: 1, problem: 'readme says 2', fixed: false },
    ],
  }, [{ file_path: 'a.ts', syntax: { status: 'pass' } }, { file_path: 'b.ts', syntax: { status: 'pass' } }]);
  const summary = qualitySummary(proposal);
  assert.deepEqual(summary.reviewIssues.map((issue) => [issue.file_path, issue.verified]), [['a.ts', false], ['b.ts', true]]);
  assert.match(summary.reviewIssues[0].note, /^not applied/);
  assert.equal(fileQuality(proposal, 'a.ts'), 'pass');
  assert.equal(fileQuality(proposal, 'b.ts'), 'warn', 'an older proposal without the field keeps counting its issues');
  assert.equal(CHECK_LABELS.tests, 'Tests touched');
});

test('qualitySummary: a legacy proposal without quality has no status', async () => {
  const { qualitySummary, qualityNote, fileQuality } = await import('./proposal.js');
  for (const legacy of [{ id: 'p', files: [{ file_path: 'a.ts' }] }, { quality: null }, { quality: [] }, null, undefined]) {
    const summary = qualitySummary(legacy);
    assert.equal(summary.status, null);
    assert.deepEqual(summary.failing, []);
    assert.deepEqual(summary.warnings, []);
    assert.equal(summary.reviewed, false);
    assert.equal(qualityNote(summary), '');
  }
  assert.equal(fileQuality({ files: [{ file_path: 'a.ts' }] }, 'a.ts'), null);
});

test('fileQuality: per-file status from syntax, checks and unfixed review issues', async () => {
  const { fileQuality } = await import('./proposal.js');
  const proposal = withQuality({
    status: 'fail',
    checks: [
      check('syntax', 'fail', { files: [{ file_path: 'a.ts', line: 30, column: 1, message: 'Unexpected end of file' }] }),
      check('style', 'warn', { files: [{ file_path: 'b.ts', line: 2, message: 'double quotes' }] }),
      check('imports', 'pass', { files: [{ file_path: 'c.ts', line: 1, message: 'added .js' }] }),
    ],
    review_issues: [{ severity: 'low', file_path: 'd.ts', line: 1, problem: 'p', fixed: false }, { severity: 'low', file_path: 'c.ts', problem: 'p', fixed: true }],
  }, [
    { file_path: 'a.ts', syntax: { status: 'fail', checker: 'esbuild', errors: [] } },
    { file_path: 'b.ts', syntax: { status: 'pass', checker: 'esbuild', errors: [] } },
    { file_path: 'c.ts', syntax: { status: 'pass', checker: 'esbuild', errors: [] } },
    { file_path: 'd.ts', syntax: { status: 'skipped', checker: 'none', errors: [] } },
    { file_path: 'readme.md', syntax: { status: 'skipped', checker: 'none', errors: [] } },
    { file_path: 'e.yml', syntax: { status: 'warn', checker: 'pyyaml', errors: [] } },
  ]);
  assert.equal(fileQuality(proposal, 'a.ts'), 'fail');
  assert.equal(fileQuality(proposal, 'b.ts'), 'warn');
  assert.equal(fileQuality(proposal, 'c.ts'), 'pass');
  assert.equal(fileQuality(proposal, 'd.ts'), 'warn');
  assert.equal(fileQuality(proposal, 'readme.md'), null, 'not checked is not a pass');
  assert.equal(fileQuality(proposal, 'e.yml'), 'warn');
  assert.equal(fileQuality(proposal, 'missing.ts'), null);
  assert.equal(fileQuality(proposal, ''), null);
});

test('buildPrBody follows a template: no second Testing section, never ticks a box, 20,000 cap', async () => {
  const { PR_BODY_LIMIT, buildPrBody, hasTestingSection } = await import('./proposal.js');
  const template = '## Description\nAdd the guard.\n\n## How Has This Been Tested?\n- [ ] npm test\n\n## Checklist\n- [ ] I have added tests';
  const body = buildPrBody({ pr_body: template, validation: ['npm test', 'npx ava test/retry.ts'] });
  assert.doesNotMatch(body, /## Testing/);
  assert.equal(body.match(/npm test/g).length, 1);
  assert.doesNotMatch(body, /\[x\]/i);
  for (const heading of ['## How to verify', '### Test plan', '## Verification', '# Testing', '**How was this tested?**', '## Validation steps', '## QA']) {
    assert.equal(hasTestingSection(`## Summary\nx\n\n${heading}\n- [ ] y`), true, heading);
    assert.doesNotMatch(buildPrBody({ pr_body: `## Summary\nx\n\n${heading}\n`, validation: ['npm test'] }), /## Testing/, heading);
  }
  for (const text of ['## Summary\nThe latest change.', '## Contest rules', 'Run the tests later.']) {
    assert.equal(hasTestingSection(text), false, text);
  }
  // A step the body already lists is not repeated even without a testing heading.
  assert.doesNotMatch(buildPrBody({ pr_body: '## Summary\nx\n\n- [ ] `npm test`', validation: ['npm test'] }), /## Testing/);
  const long = buildPrBody({ pr_body: 'x'.repeat(30000), validation: ['npm test'] });
  assert.equal(long.length, PR_BODY_LIMIT);
  assert.equal(PR_BODY_LIMIT, 20000);
  assert.match(long, /Generated with Codebase Intel and reviewed before pushing\._$/);
  assert.ok(buildPrBody({ pr_body: 'y'.repeat(12000) }).length > 12000, 'a 12,000-character body is kept whole');
});

test('commitMessageFor and titleConventionHint follow the repository conventions', async () => {
  const { CONVENTIONAL_TITLE, commitMessageFor, titleConventionHint } = await import('./proposal.js');
  assert.equal(commitMessageFor({ commit_message: 'fix: guard delay\n\nBody', title: 'T' }), 'fix: guard delay\n\nBody');
  assert.equal(commitMessageFor({ commit_message: '  ', title: 'Fix delay' }), 'Fix delay');
  assert.equal(commitMessageFor(null), 'Apply reviewed changes');
  assert.equal(commitMessageFor({ commit_message: 'x'.repeat(900) }).length, 500);

  const conventional = { commit_style: 'conventional', commit_style_source: 'recent commits (24 of 30)' };
  assert.deepEqual(titleConventionHint('fix(delay): validate input', conventional), {
    ok: true, message: 'Follows Conventional Commits, which this repository uses (recent commits (24 of 30)).',
  });
  const bad = titleConventionHint('Validate delay input', conventional);
  assert.equal(bad.ok, false);
  assert.match(bad.message, /uses Conventional Commits \(recent commits \(24 of 30\)\)\. Start the title with a type/);
  assert.equal(titleConventionHint('feat!: drop node 18', { commit_style: 'conventional' }).ok, true);
  assert.equal(titleConventionHint('Anything', { commit_style: 'plain' }), null);
  assert.equal(titleConventionHint('Anything', undefined), null);
  assert.equal(CONVENTIONAL_TITLE.test('chore: '), false);
});

test('proposalIdFor sends only a UUID', async () => {
  const { proposalIdFor } = await import('./proposal.js');
  assert.equal(proposalIdFor({ id: '335b9d5c-abc5-41b5-82e3-5a455ba6fc02' }), '335b9d5c-abc5-41b5-82e3-5a455ba6fc02');
  assert.equal(proposalIdFor({ id: 'p-crlf' }), '');
  assert.equal(proposalIdFor({}), '');
});

test('qualityFailure reads the push-time 422 and ignores other errors', async () => {
  const { qualityFailure } = await import('./proposal.js');
  const refused = qualityFailure({
    response: {
      status: 422, headers: { 'x-error-code': 'quality_failed' },
      data: { detail: { message: '2 files have syntax errors.', files: [{ file_path: 'a.ts', line: 3, column: 7, message: 'Unexpected "}"' }, {}] } },
    },
  });
  assert.deepEqual(refused, { message: '2 files have syntax errors.', files: [{ file_path: 'a.ts', line: 3, column: 7, message: 'Unexpected "}"' }] });
  // Without the exposed header, the structured detail still identifies it.
  assert.equal(qualityFailure({ response: { status: 422, headers: {}, data: { detail: { message: 'm', files: [] } } } }).message, 'm');
  assert.equal(qualityFailure({ response: { status: 422, headers: { 'X-Error-Code': 'quality_failed' }, data: { detail: 'Plain' } } }).message, 'Plain');
  // FastAPI's own validation 422 and other statuses are not quality failures.
  assert.equal(qualityFailure({ response: { status: 422, headers: {}, data: { detail: [{ msg: 'field required' }] } } }), null);
  assert.equal(qualityFailure({ response: { status: 409, headers: { 'x-error-code': 'stale_conflict' }, data: { detail: 'x' } } }), null);
  assert.equal(qualityFailure(new Error('network')), null);
});

test('findingText formats file:line:column message', async () => {
  const { findingText } = await import('./proposal.js');
  assert.equal(findingText({ file_path: 'a.ts', line: 30, column: 1, message: 'Unexpected end of file' }), 'a.ts:30:1 Unexpected end of file');
  assert.equal(findingText({ file_path: 'a.ts', line: 2, column: null, message: 'x' }), 'a.ts:2 x');
  assert.equal(findingText({ file_path: 'a.ts', line: null, column: 4, message: 'x' }), 'a.ts x');
  assert.equal(findingText({ file_path: '', message: 'only' }), 'only');
});

// ---------------------------------------------------------------- CI (G9, G10)

test('ciStateLabel, tones, counts and check outcomes', async () => {
  const { ciCheckOutcome, ciCountsText, ciStateLabel, ciStateTone } = await import('./proposal.js');
  assert.equal(ciStateLabel('success'), 'Checks passed');
  assert.equal(ciStateLabel('failure'), 'Checks failed');
  assert.equal(ciStateLabel('pending'), 'Checks running');
  assert.equal(ciStateLabel('action_required'), 'Waiting for approval');
  assert.equal(ciStateLabel('none'), 'No checks yet');
  assert.equal(ciStateLabel('weird'), 'Checks unknown');
  assert.deepEqual(['success', 'failure', 'pending', 'action_required', 'none', 'x'].map(ciStateTone), ['success', 'danger', 'info', 'warning', 'neutral', 'neutral']);
  assert.equal(ciCountsText({ total: 4, success: 3, failure: 1, pending: 0, neutral: 0, skipped: 0, action_required: 0 }), '3 passed · 1 failed');
  assert.equal(ciCountsText({ pending: 2, action_required: 1, skipped: 1 }), '2 running · 1 awaiting approval · 1 skipped');
  assert.equal(ciCountsText(undefined), '');
  assert.deepEqual(ciCheckOutcome({ status: 'completed', conclusion: 'failure' }), { label: 'Failed', tone: 'danger' });
  assert.deepEqual(ciCheckOutcome({ status: 'completed', conclusion: 'timed_out' }), { label: 'Timed out', tone: 'danger' });
  assert.deepEqual(ciCheckOutcome({ status: 'completed', conclusion: 'success' }), { label: 'Passed', tone: 'success' });
  assert.deepEqual(ciCheckOutcome({ status: 'queued', conclusion: null }), { label: 'Queued', tone: 'info' });
  assert.deepEqual(ciCheckOutcome({ status: 'in_progress', conclusion: null }), { label: 'Running', tone: 'info' });
  assert.deepEqual(ciCheckOutcome({ source: 'workflow_run', status: 'completed', conclusion: 'action_required' }), { label: 'Needs approval', tone: 'warning' });
  assert.deepEqual(ciCheckOutcome({ source: 'status', status: 'error', conclusion: null }), { label: 'Failed', tone: 'danger' });
});

test('mergeStateMessage explains merge readiness', async () => {
  const { mergeStateMessage } = await import('./proposal.js');
  assert.match(mergeStateMessage({ state: 'open', mergeable_state: 'dirty', mergeable: false }), /Merge conflicts/);
  assert.match(mergeStateMessage({ state: 'open', mergeable_state: 'behind', mergeable: true }), /behind the base branch/);
  assert.match(mergeStateMessage({ state: 'open', mergeable_state: 'blocked', mergeable: true }), /required reviews or checks/);
  assert.match(mergeStateMessage({ state: 'open', mergeable_state: 'unknown', mergeable: null }), /still checking/);
  assert.match(mergeStateMessage({ state: 'open', mergeable_state: 'clean', mergeable: true }), /No conflicts/);
  assert.equal(mergeStateMessage({ state: 'closed', merged: true, mergeable_state: 'dirty' }), 'Merged.');
  assert.equal(mergeStateMessage({ state: 'closed', merged: false }), 'Closed without merging.');
  assert.equal(mergeStateMessage(null), '');
});

test('ciPollDelay refreshes every 20 s while pending, for at most 10 minutes', async () => {
  const { CI_POLL_LIMIT_MS, CI_POLL_MS, ciPollDelay } = await import('./proposal.js');
  assert.equal(CI_POLL_MS, 20000);
  assert.equal(CI_POLL_LIMIT_MS, 600000);
  assert.equal(ciPollDelay({ state: 'pending', startedAt: 0, now: 0 }), 20000);
  assert.equal(ciPollDelay({ state: 'pending', startedAt: 0, now: 580000 }), 20000);
  assert.equal(ciPollDelay({ state: 'pending', startedAt: 0, now: 580001 }), null);
  for (const state of ['success', 'failure', 'action_required', 'none', null]) {
    assert.equal(ciPollDelay({ state, startedAt: 0, now: 0 }), null, String(state));
  }
  assert.equal(ciPollDelay({ state: 'pending', startedAt: 0, now: 0, retryAfter: 45 }), 45000, 'Retry-After stretches the wait');
  assert.equal(ciPollDelay({ state: 'pending', startedAt: Number.NaN, now: 0 }), null);
});

test('ciError explains pr-status failures', async () => {
  const { ciError } = await import('./proposal.js');
  assert.equal(ciError({ response: { status: 401, headers: {}, data: {} } }).disconnect, true);
  assert.match(ciError({ response: { status: 404, headers: { 'x-error-code': 'github_pr_not_found' }, data: { detail: 'Pull request #9 was not found in o/r.' } } }).message, /#9 was not found/);
  assert.match(ciError({ response: { status: 404, headers: { 'x-error-code': 'github_pr_not_found' }, data: {} } }).message, /could not find this pull request/);
  assert.equal(ciError({ response: { status: 404, headers: {}, data: { detail: 'Repository not found.' } } }).message, 'Repository not found.');
  const limited = ciError({ response: { status: 429, headers: { 'retry-after': '30' }, data: { detail: 'rate' } } });
  assert.equal(limited.retryAfter, 30);
  assert.match(limited.message, /30 seconds/);
  assert.match(ciError({ response: { status: 403, headers: {}, data: {} } }).message, /denied access/);
  assert.equal(ciError({ response: { status: 502, headers: {}, data: { detail: 'GitHub failed.' } } }).message, 'GitHub failed.');
  assert.match(ciError(new Error('Network Error')).message, /Could not reach the server/);
});

test('remembered pull requests keep the repository for their CI status; check links must be https', async () => {
  const { pullRequestFor, rememberPullRequest, safeHttpsUrl } = await import('./proposal.js');
  rememberPullRequest('p-ci', { pr_url: 'https://github.com/sindresorhus/ky/pull/7', pr_number: 7, repo_name: 'ky' }, 2);
  assert.equal(pullRequestFor('p-ci').repo_name, 'ky');
  assert.equal(safeHttpsUrl('https://circleci.com/gh/x/1'), 'https://circleci.com/gh/x/1');
  assert.equal(safeHttpsUrl('javascript:alert(1)'), '');
  assert.equal(safeHttpsUrl('http://ci.example/1'), '');
});
