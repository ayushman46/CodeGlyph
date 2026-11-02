import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canEdit, canManage, canShareAnswer, citationLabel, detailUnder, editBlockedReason, expiryLabel, inviteProblem, inviteSentence, linkUrl,
  isShared, membersLine, roleOf, sharedLine, splitRepos,
} from './sharing.js';

test('only a finished, written answer offers a share link', () => {
  const answer = { role: 'assistant', content: '## Direct answer\nRetries live in `source/retry.ts`.', mode: 'rag' };
  assert.equal(canShareAnswer(answer), true);
  assert.equal(canShareAnswer({ ...answer, mode: 'retrieval_fallback' }), true);
  assert.equal(canShareAnswer({ ...answer, streaming: true }), false);
  assert.equal(canShareAnswer({ ...answer, content: '  ' }), false);
  assert.equal(canShareAnswer({ ...answer, error: 'Network' }), false);
  assert.equal(canShareAnswer({ ...answer, stopped: true, partial: true }), false);
  assert.equal(canShareAnswer({ ...answer, interrupted: true }), false);
  for (const mode of ['error', 'greeting', 'editing_guidance', 'editing']) assert.equal(canShareAnswer({ ...answer, mode }), false);
  assert.equal(canShareAnswer({ ...answer, role: 'user' }), false);
  assert.equal(canShareAnswer(null), false);
});

test('citation labels name the file and the line range', () => {
  assert.equal(citationLabel({ file_path: 'source/retry.ts', start_line: 1, end_line: 3 }), 'source/retry.ts:1–3');
  assert.equal(citationLabel({ file_path: 'source/retry.ts', start_line: 7, end_line: 7 }), 'source/retry.ts:7');
  assert.equal(citationLabel({ file_path: 'README.md' }), 'README.md');
});

const owned = { id: 'a', repo_name: 'ky-http', access_role: 'owner', member_count: 2 };
const viewer = { id: 'b', repo_name: 'express', access_role: 'viewer', shared: true, owner_label: 'ann@example.com' };
const editor = { id: 'c', repo_name: 'flask', access_role: 'editor', shared: true, owner_label: 'cy@example.com' };

test('a repository without access fields (an older server) is the caller\'s own', () => {
  const legacy = { id: 'z', repo_name: 'legacy' };
  assert.equal(roleOf(legacy), 'owner');
  assert.equal(isShared(legacy), false);
  assert.equal(canManage(legacy), true);
  assert.equal(canEdit(legacy), true);
  assert.equal(roleOf({ access_role: 'admin' }), 'owner');
});

test('only the owner manages; viewers cannot edit and are told why and whom to ask', () => {
  assert.deepEqual([canManage(owned), canManage(editor), canManage(viewer)], [true, false, false]);
  assert.deepEqual([canEdit(owned), canEdit(editor), canEdit(viewer)], [true, true, false]);
  assert.equal(editBlockedReason(owned), '');
  assert.equal(editBlockedReason(editor), '');
  const reason = editBlockedReason(viewer);
  assert.match(reason, /view access/);
  assert.match(reason, /Edit & PR is off/);
  assert.match(reason, /ann@example\.com/);
  assert.match(editBlockedReason({ access_role: 'viewer' }), /Ask its owner/);
});

test('repositories split into your own and the ones shared with you, keeping order', () => {
  const second = { id: 'd', repo_name: 'httpx', access_role: 'owner' };
  const { owned: mine, shared } = splitRepos([owned, viewer, second, editor]);
  assert.deepEqual(mine.map((repo) => repo.id), ['a', 'd']);
  assert.deepEqual(shared.map((repo) => repo.id), ['b', 'c']);
  assert.deepEqual(splitRepos(), { owned: [], shared: [] });
});

test('sidebar and account lines name the role, the owner and the member count', () => {
  assert.equal(sharedLine(viewer), 'Viewer · shared by ann@example.com');
  assert.equal(sharedLine(editor), 'Editor · shared by cy@example.com');
  assert.equal(membersLine(owned), 'Shared with 2 people');
  assert.equal(membersLine({ member_count: 1 }), 'Shared with 1 person');
  assert.equal(membersLine({ member_count: 0 }), '');
  assert.equal(membersLine(viewer), '');
});

test('invite links are built from the server path and the page origin', () => {
  assert.equal(linkUrl('/invite/abc', 'https://app.example.com/'), 'https://app.example.com/invite/abc');
  assert.equal(linkUrl('invite/abc', 'http://127.0.0.1:5306'), 'http://127.0.0.1:5306/invite/abc');
});

test('expiry labels count down in days, then hours', () => {
  const now = Date.parse('2026-11-02T12:00:00Z');
  assert.equal(expiryLabel('2026-11-09T12:00:00Z', now), 'Expires in 7 days');
  assert.equal(expiryLabel('2026-11-03T12:00:00Z', now), 'Expires in 1 day');
  assert.equal(expiryLabel('2026-11-02T17:00:00Z', now), 'Expires in 5 hours');
  assert.equal(expiryLabel('2026-11-02T12:20:00Z', now), 'Expires in under an hour');
  assert.equal(expiryLabel('2026-11-02T11:00:00Z', now), 'Expired');
  assert.equal(expiryLabel('not a date', now), '');
});

test('an invite sentence says the role and the last day it works', () => {
  assert.equal(inviteSentence('viewer', '2026-11-09T12:00:00Z', 'en-GB'), 'Anyone signed in with this link can join as a viewer until 9 Nov 2026.');
  assert.equal(inviteSentence('editor', '', 'en-GB'), 'Anyone signed in with this link can join as an editor.');
});

test('a shared answer\'s unavailable message is not repeated under its heading', () => {
  const server = "This shared answer isn't available. The link may be incomplete, or the answer is no longer shared.";
  assert.equal(detailUnder('This shared answer isn’t available', server), 'The link may be incomplete, or the answer is no longer shared.');
  assert.equal(detailUnder('Title', 'Something else happened.'), 'Something else happened.');
  assert.equal(detailUnder('Title', '', 'Fallback.'), 'Fallback.');
});

test('a server message that starts with the title is not repeated under it', () => {
  assert.equal(inviteProblem(410, 'invite_revoked', 'This invite link was revoked. Ask ann for a new link.').detail, 'Ask ann for a new link.');
  assert.equal(inviteProblem(410, 'invite_expired', 'This invite link has expired. Ask ann for a new link.').detail, 'Ask ann for a new link.');
  assert.equal(inviteProblem(404, 'invite_invalid', "This invite link isn't valid. Check that you copied the whole link.").detail, 'Check that you copied the whole link.');
  assert.equal(inviteProblem(410, 'invite_revoked', 'This invite link was revoked.').detail, 'Ask the person who shared it for a new link.');
});

test('refused invite links get a clear title and the server\'s next step', () => {
  assert.equal(inviteProblem(410, 'invite_expired', 'This invite link expired on 9 Nov 2026. Ask ann for a new link.').title, 'This invite link has expired');
  assert.equal(inviteProblem(410, 'invite_revoked').title, 'This invite link was revoked');
  assert.match(inviteProblem(410, 'invite_revoked').detail, /new link/);
  assert.equal(inviteProblem(404, 'invite_invalid').title, 'This invite link isn’t valid');
  assert.equal(inviteProblem(404, undefined).title, 'This invite link isn’t valid');
  assert.equal(inviteProblem(403, 'share_removed', 'ann removed your access.').detail, 'ann removed your access.');
  assert.equal(inviteProblem(0, undefined).title, 'This invite couldn’t be opened');
});
