import assert from 'node:assert/strict';
import test from 'node:test';
import {
  compactPath, githubBlobUrl, groupByDirectory, looksLikePath, matchCitation, normaliseCitationMarkdown, parseReference, parseRepository,
  resolveReference,
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
