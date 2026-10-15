import assert from 'node:assert/strict';
import test from 'node:test';
import { classifyGithubError, retryAfterSeconds } from './githubErrors.js';

const failure = (status, detail, headers = {}) => ({ response: { status, data: detail == null ? {} : { detail }, headers } });

test('401 is the only status that disconnects GitHub', () => {
  const result = classifyGithubError(failure(401, 'expired'));
  assert.equal(result.kind, 'reconnect');
  assert.equal(result.disconnect, true);
  for (const status of [400, 403, 404, 409, 413, 429, 503]) {
    assert.equal(classifyGithubError(failure(status, 'x')).disconnect, false, String(status));
  }
});

test('403 github_permission explains access and is never retried with a fresh ticket', () => {
  const result = classifyGithubError(failure(403, 'Resource not accessible by integration', { 'x-error-code': 'github_permission' }));
  assert.equal(result.kind, 'permission');
  assert.equal(result.retryWithFreshTicket, false);
  assert.match(result.message, /Resource not accessible by integration\./);
  assert.match(result.message, /approve the Codebase Intel OAuth app/);
  const ticket = classifyGithubError(failure(403, 'Edit ticket expired'));
  assert.equal(ticket.kind, 'ticket');
  assert.equal(ticket.retryWithFreshTicket, true);
});

test('409 branch conflicts offer a new branch instead of a refresh', () => {
  const branch = classifyGithubError(failure(409, 'Branch codebase-intel/x already exists with different content.'));
  assert.equal(branch.kind, 'branch_exists');
  assert.equal(branch.action, 'new_branch');
  assert.doesNotMatch(branch.message, /Refresh from GitHub/);
  const stale = classifyGithubError(failure(409, 'src/a.ts changed on GitHub.'));
  assert.equal(stale.action, 'refresh');
  assert.match(stale.message, /Refresh from GitHub/);
  assert.equal(stale.keepKey, false);
});

test('429, 503 and 400 keep the idempotency key; 429 reports the wait', () => {
  const limited = classifyGithubError(failure(429, 'slow down', { 'retry-after': '42' }));
  assert.equal(limited.kind, 'rate_limited');
  assert.equal(limited.retryAfter, 42);
  assert.equal(limited.keepKey, true);
  assert.match(limited.message, /Wait 42 seconds/);
  assert.match(classifyGithubError(failure(429, null, { 'retry-after': '120' })).message, /Wait 2 minutes/);
  const fork = classifyGithubError(failure(503, 'Your fork is not ready yet'));
  assert.equal(fork.kind, 'unavailable');
  assert.equal(fork.keepKey, true);
  assert.match(fork.message, /Try again in a minute/);
  assert.equal(classifyGithubError(failure(400, 'GitHub could not create the pull request (HTTP 502).')).keepKey, true);
});

test('404 and 413 show the server detail', () => {
  assert.equal(classifyGithubError(failure(404, null)).kind, 'not_found');
  assert.match(classifyGithubError(failure(404, null)).message, /not visible/);
  assert.equal(classifyGithubError(failure(413, 'The request body is larger than 25 MB.')).message, 'The request body is larger than 25 MB.');
});

test('retryAfterSeconds reads seconds and HTTP dates', () => {
  assert.equal(retryAfterSeconds('30'), 30);
  assert.equal(retryAfterSeconds(new Date(1_000_000 + 90_000).toUTCString(), 1_000_000), 90);
  assert.equal(retryAfterSeconds(''), null);
  assert.equal(retryAfterSeconds('soon'), null);
});

test('body fields stand in for headers a cross-origin response does not expose', () => {
  assert.equal(classifyGithubError({ response: { status: 403, data: { detail: 'x', error_code: 'github_permission' }, headers: {} } }).kind, 'permission');
  assert.equal(classifyGithubError({ response: { status: 429, data: { detail: 'x', retry_after: 7 }, headers: {} } }).retryAfter, 7);
});

test('413 offers the patch download as the way forward', () => {
  const result = classifyGithubError(failure(413, 'Too big'));
  assert.equal(result.kind, 'too_large');
  assert.equal(result.action, 'download');
});
