import { Check, Download, FileCode2, GitFork, GitPullRequest, ListChecks, Lock, Pencil, ShieldCheck } from 'lucide-react';
import { DIFF_MARKERS, DiffStat, diffMarkerClass, diffRowClass } from './MockParts';
import Reveal from './Reveal';
import SectionHeading from './SectionHeading';
import { highlight } from './highlight';

/* One hunk of handleConfirmation.ts: 7 additions and 3 deletions. The test
 * file tab adds 5 more lines, so the change totals +12 −3 across 2 files. */
const HUNK_HEADER = '@@ -56,7 +56,11 @@ export async function handleConfirmation(args) {';

const HUNK = [
  { type: 'ctx', oldLine: 56, newLine: 56, code: '  const { evt, bookingId } = args;' },
  { type: 'del', oldLine: 57, code: '  const booking = await getBooking(bookingId);' },
  { type: 'add', newLine: 57, code: '  const booking = await getBooking(bookingId, {' },
  { type: 'add', newLine: 58, code: '    select: { id: true, uid: true, status: true },' },
  { type: 'add', newLine: 59, code: '  });' },
  { type: 'ctx', oldLine: 58, newLine: 60, code: '  if (!booking) return;' },
  { type: 'del', oldLine: 59, code: '  await notify(evt);' },
  { type: 'add', newLine: 61, code: '  if (isAccepted(booking)) return;' },
  { type: 'add', newLine: 62, code: '  await markAccepted(booking.id);' },
  { type: 'add', newLine: 63, code: '  await notify(evt, booking.uid);' },
  { type: 'ctx', oldLine: 60, newLine: 64, code: '' },
  { type: 'del', oldLine: 61, code: '  await scheduleReminders(evt);' },
  { type: 'add', newLine: 65, code: '  await scheduleReminders(evt, booking.uid);' },
  { type: 'ctx', oldLine: 62, newLine: 66, code: '  return { confirmed: true };' },
];

const gutterClass = (type) => (
  type === 'add'
    ? 'bg-diff-gutter-add text-diff-add-ink/80'
    : type === 'del'
      ? 'bg-diff-gutter-del text-diff-del-ink/80'
      : 'text-code-muted'
);

/* GitHub-style diffstat blocks: 12 of 15 changed lines are additions. */
const STAT_BLOCKS = ['add', 'add', 'add', 'add', 'del'];

const DiffShowcase = () => (
  <figure className="relative min-w-0">
    <figcaption className="sr-only">
      Example review screen for a pull request titled &ldquo;Don&rsquo;t resend emails on repeat confirmation&rdquo;: 2 files
      changed with 12 additions and 3 deletions. The unified diff of handleConfirmation.ts adds an early return for bookings that
      are already accepted. Every hunk is marked as validated, and the change can be edited, downloaded as a .patch, or opened as a
      pull request.
    </figcaption>

    <div aria-hidden="true" className="accent-glow pointer-events-none absolute -inset-y-6 inset-x-0 -z-10 opacity-70 blur-2xl sm:-inset-x-6" />

    <div aria-hidden="true" className="overflow-hidden rounded-2xl border border-code-line bg-code shadow-[var(--shadow-lift)]">
      <div className="flex items-center justify-between gap-3 border-b border-code-line bg-code-2 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <GitPullRequest className="h-4 w-4 shrink-0 text-diff-add-ink" />
          <span className="truncate text-[13px] font-medium text-code-ink">Don&rsquo;t resend emails on repeat confirmation</span>
        </div>
        <div className="flex shrink-0 rounded-lg border border-code-line p-0.5 text-[11px]">
          <span className="rounded-md bg-code-line px-2 py-0.5 font-medium text-code-ink">Unified</span>
          <span className="px-2 py-0.5 text-code-muted">Split</span>
        </div>
      </div>

      <div className="flex items-center gap-3 border-b border-code-line px-4 py-2 text-[11.5px]">
        <span className="text-code-ink">2 files changed</span>
        <DiffStat added={12} removed={3} />
        <span className="flex gap-0.5">
          {STAT_BLOCKS.map((type, index) => (
            <span key={index} className={`h-2 w-2 rounded-[2px] ${type === 'add' ? 'bg-diff-add-strong' : 'bg-diff-del-strong'}`} />
          ))}
        </span>
      </div>

      <div className="flex border-b border-code-line text-[12px]">
        <span className="-mb-px flex min-w-0 items-center gap-2 border-b-2 border-accent px-4 py-2 text-code-ink">
          <FileCode2 className="h-3.5 w-3.5 shrink-0 text-code-muted" />
          <span className="truncate font-mono">handleConfirmation.ts</span>
          <DiffStat added={7} removed={3} className="text-[11px]" />
        </span>
        <span className="hidden min-w-0 items-center gap-2 px-4 py-2 text-code-muted sm:flex">
          <FileCode2 className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate font-mono">handleConfirmation.test.ts</span>
          <DiffStat added={5} className="text-[11px]" />
        </span>
      </div>

      <div className="flex items-center justify-between gap-3 border-b border-code-line px-4 py-2 text-[11.5px]">
        <span className="flex min-w-0 font-mono">
          <span className="hidden min-w-0 truncate text-code-muted sm:block">packages/features/bookings/lib/</span>
          <span className="shrink-0 text-code-ink">handleConfirmation.ts</span>
        </span>
        <span className="flex shrink-0 items-center gap-3">
          <span className="inline-flex items-center gap-1 text-diff-add-ink">
            <Check className="h-3.5 w-3.5" />
            Validated
          </span>
          <span className="inline-flex items-center gap-1 rounded-md border border-code-line px-1.5 py-0.5 text-code-ink">
            <Pencil className="h-3 w-3" />
            Edit
          </span>
        </span>
      </div>

      {/* The hunk header sits outside the scroller and truncates, so only real
          code lines can make the diff scroll sideways (and only on phones). */}
      <div className="mt-1 flex bg-code-2 font-mono text-[11px] leading-6 text-code-muted sm:text-[12px]">
        <span className="w-[4.5rem] shrink-0 sm:w-[5.5rem]" />
        <span className="min-w-0 truncate pr-4">{HUNK_HEADER}</span>
      </div>

      {/* Explicit tabIndex keeps this decorative scroller out of the Tab
          order (Chrome otherwise makes scrollers keyboard-focusable). */}
      <div className="code-scroll overflow-x-auto" tabIndex={-1}>
        <div className="code-tokens min-w-max pb-1 font-mono text-[11px] leading-6 sm:text-[12px]">
          {HUNK.map((line, index) => (
            <div key={index} className={`flex ${diffRowClass(line.type)}`}>
              <span className={`w-9 shrink-0 select-none pr-2 text-right tabular-nums sm:w-11 ${gutterClass(line.type)}`}>{line.oldLine ?? ''}</span>
              <span className={`w-9 shrink-0 select-none pr-2 text-right tabular-nums sm:w-11 ${gutterClass(line.type)}`}>{line.newLine ?? ''}</span>
              <span className={`w-6 shrink-0 select-none text-center ${diffMarkerClass(line.type)}`}>{DIFF_MARKERS[line.type]}</span>
              <span className="whitespace-pre pr-6">{highlight(line.code)}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-3 border-t border-code-line bg-code-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <span className="flex min-w-0 items-center gap-1.5 text-[11.5px] text-code-muted">
          <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-diff-add-ink" />
          2 of 2 hunks validated against the current files
        </span>
        <div className="flex shrink-0 gap-2">
          <span className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-code-line px-3 text-[12px] font-medium text-code-ink">
            <Download className="h-3.5 w-3.5" />
            .patch
          </span>
          <span className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-accent px-3 text-[12px] font-medium text-accent-ink">
            <GitPullRequest className="h-3.5 w-3.5" />
            Create pull request
          </span>
        </div>
      </div>
    </div>
  </figure>
);

const POINTS = [
  {
    icon: ListChecks,
    title: 'Plans across files.',
    body: 'Describe a change or paste a GitHub issue URL. The agent plans edits across files, including call sites and tests.',
  },
  {
    icon: ShieldCheck,
    title: 'Validated hunks.',
    body: 'It reads the current files from GitHub, writes exact search/replace hunks and checks every one against the real file.',
  },
  {
    icon: Pencil,
    title: 'Edit before pushing.',
    body: 'Review the green and red diff, unified or split, and adjust it until it is right.',
  },
  {
    icon: GitFork,
    title: 'Branch or fork.',
    body: 'One click opens a pull request from a branch in your repository, or from a fork when you lack write access.',
  },
  {
    icon: Download,
    title: 'Download a .patch.',
    body: 'Prefer your own tooling? Take the patch and apply it locally.',
  },
];

/* Phones get heading, then the diff, then the details, so the payoff is
 * visible without reading five paragraphs first. From lg the diff sits in a
 * right-hand column spanning both rows. */
const PullRequestSection = () => (
  <section id="pull-requests" aria-labelledby="pr-title" className="scroll-mt-20 py-20 sm:py-28">
    <div className="content-shell grid grid-cols-1 gap-y-10 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:grid-rows-[auto_1fr] lg:gap-x-16 lg:gap-y-8">
      <Reveal className="lg:col-start-1 lg:row-start-1">
        <SectionHeading
          id="pr-title"
          align="left"
          eyebrow="Code editing"
          title="A red/green diff you can edit. Then a real PR."
          description="Describe the change. Codebase Intel edits every file it touches, checks each hunk against the real source, and shows the diff. Edit it, approve it, and one click opens the pull request on GitHub."
        />
      </Reveal>

      <Reveal delay={0.1} className="min-w-0 lg:col-start-2 lg:row-span-2 lg:row-start-1 lg:self-center">
        <DiffShowcase />
      </Reveal>

      <Reveal className="lg:col-start-1 lg:row-start-2">
        <ul className="space-y-4">
          {POINTS.map(({ icon: Icon, title, body }) => (
            <li key={title} className="flex gap-3.5">
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-line bg-surface text-accent-strong shadow-sm">
                <Icon className="h-4 w-4" aria-hidden="true" />
              </span>
              <p className="text-[15px] leading-relaxed text-ink-2">
                <span className="font-semibold text-ink">{title}</span> {body}
              </p>
            </li>
          ))}
        </ul>
        <p className="mt-8 inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3.5 py-1.5 text-[13px] font-medium text-ink shadow-sm">
          <Lock className="h-3.5 w-3.5 text-accent-strong" aria-hidden="true" />
          Nothing is pushed without your review.
        </p>
      </Reveal>
    </div>
  </section>
);

export default PullRequestSection;
