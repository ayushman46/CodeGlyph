import assert from 'node:assert/strict';
import test from 'node:test';
import {
  answerLinkTarget, commitFromCitations, compactPath, githubBlobUrl, groupByDirectory, looksLikePath, markdownWithPermalinks,
  matchCitation, normaliseCitationMarkdown, parseReference, parseRepository, referenceReasonLabel, resolveReference,
  summariseReferences,
} from './citations.js';

test('looksLikePath accepts repository paths and rejects prose', () => {
  for (const value of ['src/app.ts', 'apps/web/lib/auth/session.ts', 'Dockerfile', 'package.json', 'main.py', 'packages/ui/Button.tsx']) {
    assert.equal(looksLikePath(value), true, value);
  }
  for (const value of ['e.g.', 'v1.2', 'https://x.com/a.js', '@scope/pkg', '../etc/passwd', 'hello world.ts', '', 'a/b']) {
    assert.equal(looksLikePath(value), false, value);
  }
});

test('parseReference understands every citation form', () => {
  assert.deepEqual(parseReference('src/a.ts:L10-L20'), { path: 'src/a.ts', start: 10, end: 20 });
  assert.deepEqual(parseReference('src/a.ts:10-20'), { path: 'src/a.ts', start: 10, end: 20 });
  assert.deepEqual(parseReference('src/a.ts:L7'), { path: 'src/a.ts', start: 7, end: 7 });
  assert.deepEqual(parseReference('src/a.ts (L3-L9)'), { path: 'src/a.ts', start: 3, end: 9 });
  assert.deepEqual(parseReference('src/a.ts'), { path: 'src/a.ts', start: null, end: null });
  assert.deepEqual(parseReference('src/a.ts:L20-L10'), { path: 'src/a.ts', start: 20, end: 20 });
  assert.equal(parseReference('useState'), null);
  assert.equal(parseReference('npm run build'), null);
});

test('normaliseCitationMarkdown wraps references but never touches code', () => {
  const input = [
    '- **apps/web/auth.ts (L4-L18):** handles login',
    'See apps/web/lib/session.ts:L10-L30, then `already/wrapped.ts:L1`.',
    '```ts',
    'const x = "src/not/a/citation.ts:L1-L2";',
    '```',
  ].join('\n');
  const output = normaliseCitationMarkdown(input);
  assert.match(output, /`apps\/web\/auth\.ts:L4-L18`/);
  assert.match(output, /See `apps\/web\/lib\/session\.ts:L10-L30`, then `already\/wrapped\.ts:L1`\./);
  assert.match(output, /const x = "src\/not\/a\/citation\.ts:L1-L2";/);
  assert.equal(normaliseCitationMarkdown('no refs here'), 'no refs here');
});

test('GitHub links pin the commit and line range', () => {
  assert.deepEqual(parseRepository('https://github.com/calcom/cal.com.git'), { owner: 'calcom', repo: 'cal.com', fullName: 'calcom/cal.com' });
  assert.equal(
    githubBlobUrl('https://github.com/calcom/cal.com.git', 'abc123', 'apps/web/[slug].tsx', 4, 9),
    'https://github.com/calcom/cal.com/blob/abc123/apps/web/%5Bslug%5D.tsx#L4-L9',
  );
  assert.equal(githubBlobUrl('not a url', 'x', 'a.ts'), null);
});

test('groupByDirectory and matchCitation preserve relevance order', () => {
  const citations = [
    { file_path: 'src/auth/login.ts', start_line: 1, end_line: 40 },
    { file_path: 'src/db/schema.ts', start_line: 1, end_line: 20 },
    { file_path: 'src/auth/session.ts', start_line: 10, end_line: 30 },
  ];
  const groups = groupByDirectory(citations);
  assert.deepEqual(groups.map((group) => group.directory), ['src/auth', 'src/db']);
  assert.deepEqual(groups[0].items.map((item) => item.index), [0, 2]);
  assert.equal(matchCitation(citations, { path: 'src/auth/session.ts', start: 12 }), 2);
  assert.equal(matchCitation(citations, { path: 'session.ts', start: null }), 2);
  assert.equal(matchCitation(citations, { path: 'missing.ts', start: 1 }), -1);
});

test('dotted identifiers and member accesses never become file references (FE-1 / cite-where-05 / CITE-HOW-06)', () => {
  for (const value of [
    'BookingStatus.ACCEPTED', 'prisma.booking.update', 'prisma.booking', 'booking.status', 'sdkActionManager.fire',
    'bookingRepository.update', 'RegularBookingService.create', 'req.body', 'process.env', 'console.log', 'React.FC',
    'z.object', 'ctx.user.id', 'Promise.all', 'Date.now', 'trpc.viewer.bookings.get', 'next.config', 'module.exports',
    'window.location.href', 'e.g.', 'v1.2',
  ]) {
    assert.equal(parseReference(value), null, value);
    assert.equal(resolveReference(value, { citations: [] }), null, value);
  }
});

test('Next.js route groups, catch-alls and odd root files are links (FE-2)', () => {
  assert.deepEqual(parseReference('apps/web/app/(use-page-wrapper)/settings/page.tsx:L1-L20'), {
    path: 'apps/web/app/(use-page-wrapper)/settings/page.tsx', start: 1, end: 20,
  });
  assert.deepEqual(parseReference('apps/web/pages/api/auth/[...nextauth].ts:L4'), { path: 'apps/web/pages/api/auth/[...nextauth].ts', start: 4, end: 4 });
  assert.deepEqual(parseReference('apps/web/app/[[...slug]]/page.tsx'), { path: 'apps/web/app/[[...slug]]/page.tsx', start: null, end: null });
  assert.deepEqual(parseReference('apps/web/pages/[id].tsx (L3-L9)'), { path: 'apps/web/pages/[id].tsx', start: 3, end: 9 });
  assert.deepEqual(parseReference('packages/platform/README-OAUTH2.MD:L1-L40'), { path: 'packages/platform/README-OAUTH2.MD', start: 1, end: 40 });
  assert.equal(looksLikePath('apps/../etc/passwd.ts'), false);
  assert.equal(looksLikePath('apps/./x.ts'), false);
  const output = normaliseCitationMarkdown('See apps/web/app/(booking-page-wrapper)/x/page.tsx:L1-L4 and (apps/b/[id].tsx:L3). Also Dockerfile:L1-L9 and apps/api/v2/Dockerfile:L2.');
  assert.match(output, /See `apps\/web\/app\/\(booking-page-wrapper\)\/x\/page\.tsx:L1-L4` and \(`apps\/b\/\[id\]\.tsx:L3`\)\./);
  assert.match(output, /`Dockerfile:L1-L9`/);
  assert.match(output, /`apps\/api\/v2\/Dockerfile:L2`/);
});

test('common citation variants keep their file and lines (FE-10)', () => {
  assert.deepEqual(parseReference('src/a.ts:L10-L20,'), { path: 'src/a.ts', start: 10, end: 20 });
  assert.deepEqual(parseReference('**src/a.ts:L4-L18**'), { path: 'src/a.ts', start: 4, end: 18 });
  assert.deepEqual(parseReference('docs/User Guide.md:L1-L3'), { path: 'docs/User Guide.md', start: 1, end: 3 });
  assert.equal(parseReference('hello world.ts'), null);
  const output = normaliseCitationMarkdown('**apps/a.ts:L4-L18** then `apps/c.ts` from line 133 to 392, `apps/d.ts` lines 10-20 and `x.y` line 3. Root AGENTS.md:L1-L5.');
  assert.match(output, /\*\*`apps\/a\.ts:L4-L18`\*\*/);
  assert.match(output, /`apps\/c\.ts:L133-L392` from line 133 to 392/);
  assert.match(output, /`apps\/d\.ts:L10-L20` lines 10-20/);
  assert.match(output, /`x\.y` line 3/);
  assert.match(output, /`AGENTS\.md:L1-L5`/);
});

test('resolveReference: bare names and spaced paths need a citation; directory paths stand alone', () => {
  const citations = [{ file_path: 'packages/ui/components.ts', start_line: 1, end_line: 9 }];
  assert.equal(resolveReference('components.ts', { citations: [] }), null);
  assert.equal(resolveReference('getLuckyUser.test.ts', { citations }), null);
  assert.equal(resolveReference('components.ts', { citations }).status, 'cited');
  assert.equal(resolveReference('docs/User Guide.md', { citations }), null);
  assert.deepEqual(resolveReference('apps/web/a.ts:L2', { citations: [] }), { status: 'path', reference: { path: 'apps/web/a.ts', start: 2, end: 2 } });
});

test('resolveReference trusts server answer_references when present', () => {
  const answerReferences = [
    { text: 'apps/a.ts:L1-L4', file_path: 'apps/a.ts', start_line: 1, end_line: 4, valid: true },
    { text: 'ghost.ts:L1', file_path: 'apps/ghost.ts', start_line: 1, end_line: 1, valid: false },
    { text: 'Dockerfile', file_path: 'Dockerfile', start_line: null, end_line: null, valid: true },
  ];
  assert.deepEqual(resolveReference('apps/a.ts:L1-L4', { answerReferences }), { status: 'verified', reference: { path: 'apps/a.ts', start: 1, end: 4 } });
  // Matched by path and start line when the text differs (markdown normalisation).
  assert.deepEqual(resolveReference('a.ts:L1-L4', { answerReferences }), { status: 'verified', reference: { path: 'apps/a.ts', start: 1, end: 4 } });
  assert.equal(resolveReference('ghost.ts:L1', { answerReferences }).status, 'unverified');
  assert.deepEqual(resolveReference('Dockerfile', { answerReferences }), { status: 'verified', reference: { path: 'Dockerfile', start: null, end: null } });
  // Not listed by the server: plain code, even if it looks like a path.
  assert.equal(resolveReference('apps/other.ts:L3', { answerReferences }), null);
  assert.equal(resolveReference('BookingStatus.ACCEPTED', { answerReferences }), null);
  assert.equal(resolveReference('apps/a.ts:L1-L4', { answerReferences: [] }), null);
});

test('compactPath keeps the trailing folders that tell same-named files apart (FV-10)', () => {
  assert.equal(compactPath('src/a.ts'), 'src/a.ts');
  assert.equal(compactPath('packages/app-store/googlecalendar/lib/CalendarService.ts', 36), '…/lib/CalendarService.ts');
  assert.equal(compactPath('packages/app-store/googlecalendar/lib/CalendarService.ts', 44), '…/googlecalendar/lib/CalendarService.ts');
  assert.equal(compactPath('packages/app-store/office365calendar/lib', 30), '…/office365calendar/lib');
});

test('unverified references carry what is actually wrong (BUG-07)', () => {
  const answerReferences = [
    { text: 'package.json:L92-L98', file_path: 'package.json', start_line: 92, end_line: 98, valid: false, reason: 'range_outside_evidence' },
    { text: 'view.js', file_path: null, start_line: null, end_line: null, valid: false, reason: 'ambiguous', candidates: ['lib/view.js', 'test/view.js'] },
    { text: 'lib/gone.js:L3', file_path: null, start_line: 3, end_line: 3, valid: false, reason: 'not_in_index' },
    { text: 'package.json:L140', file_path: 'package.json', start_line: 140, end_line: 140, valid: false, reason: 'lines_outside_file', file_lines: 99 },
    { text: 'lib/router.js:L1-L2', file_path: 'lib/router.js', start_line: 1, end_line: 2, valid: true },
  ];
  assert.deepEqual(resolveReference('package.json:L92-L98', { answerReferences }), {
    status: 'outside_evidence', reference: { path: 'package.json', start: 92, end: 98 },
    reason: 'range_outside_evidence', label: 'exists in the index but was not part of this answer’s evidence',
  });
  const ambiguous = resolveReference('view.js', { answerReferences });
  assert.equal(ambiguous.status, 'unverified');
  assert.equal(ambiguous.label, 'matches several files');
  assert.deepEqual(ambiguous.candidates, ['lib/view.js', 'test/view.js']);
  assert.equal(resolveReference('lib/gone.js:L3', { answerReferences }).label, 'not in the indexed snapshot');
  assert.equal(resolveReference('package.json:L140', { answerReferences }).label, 'the file exists but has only 99 lines');
  assert.equal(referenceReasonLabel({ reason: 'lookup_failed' }), 'could not be checked: the index lookup failed');
  assert.deepEqual(summariseReferences(answerReferences), {
    total: 5, verified: 1, unverified: 4, repaired: 0, inIndex: 2,
    byReason: { range_outside_evidence: 1, ambiguous: 1, not_in_index: 1, lines_outside_file: 1 },
  });
});

test('copied answers link every resolvable reference to its commit-pinned GitHub lines (FEAT-09)', () => {
  const sha = '3c7baaa0e6bd31ebbd3f2b0f0f7ec6c2a1b2c3d4';
  const answerReferences = [
    { text: 'src/requests/models.py:L245', file_path: 'src/requests/models.py', start_line: 245, end_line: 245, valid: true },
    { text: 'models.py:L576-L652', file_path: 'src/requests/models.py', start_line: 576, end_line: 652, valid: true },
    { text: 'src/requests/utils.py:L900-L910', file_path: 'src/requests/utils.py', start_line: 900, end_line: 910, valid: false, reason: 'range_outside_evidence' },
    { text: 'src/requests/ghost.py:L1', file_path: null, start_line: 1, end_line: 1, valid: false, reason: 'not_in_index' },
  ];
  const markdown = [
    '## Draft GitHub comment',
    'The crash starts in `src/requests/models.py:L245` (see also `models.py:L576-L652`),',
    'then `src/requests/utils.py:L900-L910`; `src/requests/ghost.py:L1` and `to_native_string` are not linked.',
    '```py',
    'path = "src/requests/models.py:L245"',
    '```',
  ].join('\n');
  const copied = markdownWithPermalinks(markdown, { githubUrl: 'https://github.com/psf/requests.git', commit: sha, answerReferences });
  const base = `https://github.com/psf/requests/blob/${sha}`;
  assert.ok(copied.includes(`[\`src/requests/models.py:L245\`](${base}/src/requests/models.py#L245)`));
  assert.ok(copied.includes(`[\`models.py:L576-L652\`](${base}/src/requests/models.py#L576-L652)`));
  assert.ok(copied.includes(`[\`src/requests/utils.py:L900-L910\`](${base}/src/requests/utils.py#L900-L910)`));
  // Not in the index: no link to a file that does not exist; names and code blocks stay as they were.
  assert.ok(copied.includes('`src/requests/ghost.py:L1`') && !copied.includes('ghost.py#L1'));
  assert.ok(copied.includes('`to_native_string`'));
  assert.ok(copied.includes('path = "src/requests/models.py:L245"'));
  // Running it twice changes nothing.
  assert.equal(markdownWithPermalinks(copied, { githubUrl: 'https://github.com/psf/requests', commit: sha, answerReferences }), copied);
  // Without a repository or commit the Markdown is returned unlinked.
  assert.ok(!markdownWithPermalinks(markdown, { answerReferences }).includes('https://'));
  // Older answers without server checks link only references among their sources.
  const citations = [{ file_path: 'src/requests/models.py', start_line: 240, end_line: 260, github_url: `${base}/src/requests/models.py#L240-L260` }];
  const legacy = markdownWithPermalinks('See `models.py:L245` and `other/file.py:L3`.', { githubUrl: 'https://github.com/psf/requests', commit: sha, citations });
  assert.equal(legacy, `See [\`models.py:L245\`](${base}/src/requests/models.py#L245) and \`other/file.py:L3\`.`);
});

test('the link target is the commit the answer was written against', () => {
  const citations = [{ github_url: 'https://github.com/psf/requests/blob/abc1234def/src/a.py#L1-L2' }];
  assert.equal(commitFromCitations(citations), 'abc1234def');
  assert.equal(commitFromCitations([{ github_url: null }]), null);
  const repo = { github_url: 'https://github.com/psf/requests.git', commit_sha: 'beef999' };
  assert.deepEqual(answerLinkTarget({ citations }, repo), { githubUrl: repo.github_url, commit: 'abc1234def' });
  assert.deepEqual(answerLinkTarget({ snapshot: { commit_sha: 'cafe123', github_url: repo.github_url } }, repo), { githubUrl: repo.github_url, commit: 'cafe123' });
  assert.deepEqual(answerLinkTarget({}, repo), { githubUrl: repo.github_url, commit: 'beef999' });
  assert.equal(answerLinkTarget({}, { github_url: 'not github' }), null);
});

test('a server-resolved path matches only its own citation (round 3: index.js opened examples/mvc/index.js)', () => {
  const citations = [{ file_path: 'examples/mvc/index.js', start_line: 1, end_line: 95 }, { file_path: 'lib/express.js', start_line: 1, end_line: 40 }];
  assert.equal(matchCitation(citations, { path: 'index.js', start: null }), 0);
  assert.equal(matchCitation(citations, { path: 'index.js', start: null }, { exact: true }), -1);
  assert.equal(matchCitation(citations, { path: 'lib/express.js', start: 3 }, { exact: true }), 1);
});
