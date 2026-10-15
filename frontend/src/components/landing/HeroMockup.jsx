import { motion, useReducedMotion } from 'framer-motion';
import { Check, ChevronDown, ChevronRight, CornerDownLeft, Folder, GitPullRequest, Pencil, Plus, Search } from 'lucide-react';
import { BrandGlyph, CitationChip, DiffStat, MiniDiff, WindowDots } from './MockParts';
import { EASE_OUT } from './Reveal';

const SOURCE_FOLDERS = [
  { path: 'packages/features/bookings', count: 3 },
  { path: 'packages/trpc/…/bookings', count: 2 },
  { path: 'apps/web', count: 1 },
];

/* An excerpt of the same change shown in full in the pull request section. */
const DIFF_PREVIEW = [
  { type: 'ctx', code: '  if (!booking) return;' },
  { type: 'del', code: '  await notify(evt);' },
  { type: 'add', code: '  if (isAccepted(booking)) return;' },
  { type: 'add', code: '  await markAccepted(booking.id);' },
  { type: 'add', code: '  await notify(evt, booking.uid);' },
];

/* Opacity and translate only (no scale), so the large framed mockup is
 * composited rather than re-rasterised on every frame, and the conversation
 * finishes appearing about a second after load. */
const frameVariants = {
  hidden: { opacity: 0, y: 24 },
  show: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.6, ease: EASE_OUT, delay: 0.12, delayChildren: 0.35, staggerChildren: 0.1 },
  },
};

const itemVariants = {
  hidden: { opacity: 0, y: 10 },
  show: { opacity: 1, y: 0, transition: { duration: 0.4, ease: EASE_OUT } },
};

const SidebarLabel = ({ children }) => (
  <span className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-ink-2">{children}</span>
);

const RepoSidebar = ({ reduceMotion }) => (
  <div className="hidden min-w-0 flex-col gap-3 border-r border-line bg-surface-2/50 p-3 md:flex">
    <div className="flex items-center justify-between px-1">
      <SidebarLabel>Repositories</SidebarLabel>
      <span className="flex h-5 w-5 items-center justify-center rounded-md border border-line bg-surface text-ink-3">
        <Plus className="h-3 w-3" />
      </span>
    </div>

    <div className="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-2 py-1.5 text-[11px] text-ink-3">
      <Search className="h-3 w-3 shrink-0" />
      <span className="truncate">Find a repository</span>
    </div>

    <ul className="space-y-1">
      <li className="rounded-lg border border-line bg-surface px-2.5 py-2 shadow-sm">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-success ring-[3px] ring-success-soft" />
          <span className="truncate font-mono text-[11.5px] font-semibold text-ink">calcom/cal.com</span>
        </div>
        <p className="mt-1 pl-3.5 text-[10.5px] text-ink-3">
          <span className="font-medium text-success">Ready</span> · main
        </p>
      </li>

      <li className="rounded-lg px-2.5 py-2">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-accent" />
          <span className="truncate font-mono text-[11.5px] font-medium text-ink-2">vercel/next.js</span>
        </div>
        <div className="mt-1.5 pl-3.5">
          <div className="flex items-center justify-between text-[10px] text-ink-2">
            <span>Semantic index</span>
            <span className="font-mono tabular-nums">64%</span>
          </div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-surface-3">
            <motion.div
              className="h-full rounded-full bg-accent"
              initial={reduceMotion ? false : { width: '38%' }}
              animate={{ width: '64%' }}
              transition={{ duration: 2.2, delay: 1.1, ease: 'easeOut' }}
              style={{ width: '64%' }}
            />
          </div>
          <p className="mt-1 flex items-center gap-1 text-[10px] text-ink-2">
            <Check className="h-2.5 w-2.5 text-success" />
            Keyword search ready
          </p>
        </div>
      </li>

      <li className="rounded-lg px-2.5 py-2">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-success" />
          <span className="truncate font-mono text-[11.5px] font-medium text-ink-2">facebook/react</span>
        </div>
        <p className="mt-1 pl-3.5 text-[10.5px] text-ink-2">Ready</p>
      </li>
    </ul>

    <div className="mt-auto rounded-lg border border-line bg-surface p-2.5">
      <div className="flex items-center justify-between text-[10px]">
        <span className="font-medium text-ink-2">Explorer plan</span>
        <span className="font-mono tabular-nums text-ink-3">38 / 200 MB</span>
      </div>
      <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-surface-3">
        <div className="h-full w-[19%] rounded-full bg-ink-3" />
      </div>
    </div>
  </div>
);

const InlineCode = ({ children }) => (
  <code className="rounded border border-line bg-surface-2 px-1 py-px font-mono text-[0.85em] text-ink">{children}</code>
);

const ChatPanel = () => (
  <div className="flex min-w-0 flex-col">
    <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5 sm:px-5">
      <div className="flex min-w-0 items-center gap-2">
        <span className="truncate font-mono text-[12.5px] font-semibold text-ink">calcom/cal.com</span>
        <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-success-soft px-1.5 py-px text-[10px] font-medium text-success">
          <span className="h-1 w-1 rounded-full bg-current" />
          Ready
        </span>
      </div>
      <span className="hidden shrink-0 items-center gap-1 rounded-lg border border-line bg-surface px-2 py-1 text-[11px] text-ink-2 sm:inline-flex">
        General questions
        <ChevronDown className="h-3 w-3 text-ink-3" />
      </span>
    </div>

    <div className="flex-1 space-y-4 px-4 py-5 sm:px-5">
      <motion.div variants={itemVariants} className="flex justify-end">
        <p className="max-w-[85%] rounded-2xl rounded-br-md border border-line bg-surface-2 px-3.5 py-2 text-[12.5px] text-ink sm:text-[13px]">
          Where is booking confirmation handled?
        </p>
      </motion.div>

      <motion.div variants={itemVariants} className="flex gap-3">
        <BrandGlyph className="mt-0.5 h-6 w-6 shrink-0" />
        <div className="min-w-0 flex-1 space-y-2.5 text-[12.5px] leading-relaxed text-ink-2 sm:text-[13px]">
          <p>
            Confirmation starts in the bookings confirm handler, which calls <InlineCode>handleConfirmation</InlineCode> when an
            organizer accepts a booking that requires confirmation.{' '}
            <CitationChip dir="packages/trpc/server/routers/viewer/bookings/" dirFrom="xl" file="confirm.handler.ts" lines="L31-L96" />
          </p>
          <p>
            <InlineCode>handleConfirmation</InlineCode> marks the booking as accepted, sends the confirmation emails and runs the
            event type&rsquo;s workflows and webhooks.{' '}
            <CitationChip dir="packages/features/bookings/lib/" dirFrom="xl" file="handleConfirmation.ts" lines="L42-L118" />
          </p>
        </div>
      </motion.div>

      <motion.div variants={itemVariants} className="rounded-xl border border-line bg-surface-2/60 p-2.5 sm:ml-9">
        <div className="flex items-center justify-between gap-2 text-[11px]">
          <span className="font-medium text-ink-2">Sources · 6 files in 3 folders</span>
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-3" />
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {SOURCE_FOLDERS.map((folder) => (
            <span key={folder.path} className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-line bg-surface px-1.5 py-0.5 font-mono text-[10.5px] text-ink-2">
              <Folder className="h-3 w-3 shrink-0 text-ink-3" />
              <span className="truncate">{folder.path}</span>
              <span className="rounded bg-surface-3 px-1 text-[9.5px] tabular-nums text-ink-2">{folder.count}</span>
            </span>
          ))}
        </div>
      </motion.div>
    </div>

    <div className="border-t border-line p-3 sm:px-5">
      <div className="flex items-center gap-2 rounded-xl border border-line bg-surface py-1.5 pl-3 pr-1.5 shadow-sm">
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-3">Ask a follow-up about calcom/cal.com…</span>
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-ink">
          <CornerDownLeft className="h-3.5 w-3.5" />
        </span>
      </div>
    </div>
  </div>
);

const DiffPanel = () => (
  <div className="hidden min-w-0 flex-col gap-3 border-l border-line bg-surface-2/50 p-4 lg:flex">
    <motion.div variants={itemVariants} className="flex items-center justify-between gap-2">
      <SidebarLabel>Proposed change</SidebarLabel>
      <span className="inline-flex items-center gap-1 rounded-full bg-success-soft px-2 py-0.5 text-[10.5px] font-medium text-success">
        <Check className="h-3 w-3" />
        Validated
      </span>
    </motion.div>

    <motion.p variants={itemVariants} className="text-[12px] leading-snug text-ink-2">
      Don&rsquo;t resend emails when a booking is confirmed twice.
    </motion.p>

    <motion.div variants={itemVariants} className="overflow-hidden rounded-xl border border-code-line bg-code">
      <div className="flex items-center justify-between gap-2 border-b border-code-line bg-code-2 px-3 py-2 text-[11px]">
        <span className="truncate font-mono text-code-ink">handleConfirmation.ts</span>
        <DiffStat added={7} removed={3} />
      </div>
      <MiniDiff lines={DIFF_PREVIEW} />
    </motion.div>

    <motion.p variants={itemVariants} className="flex items-center justify-between text-[11px] text-ink-2">
      <span>2 files changed</span>
      <span className="inline-flex items-center gap-1">
        <Pencil className="h-3 w-3" />
        Editable
      </span>
    </motion.p>

    <motion.div variants={itemVariants} className="mt-auto space-y-2">
      <span className="flex h-8 w-full items-center justify-center gap-1.5 rounded-lg bg-accent text-[12.5px] font-medium text-accent-ink shadow-sm">
        <GitPullRequest className="h-3.5 w-3.5" />
        Create pull request
      </span>
      <span className="block text-center text-[11px] text-ink-2">or download .patch</span>
    </motion.div>
  </div>
);

const HeroMockup = () => {
  const reduceMotion = useReducedMotion();
  return (
    <figure className="relative mx-auto w-full max-w-6xl">
      <figcaption className="sr-only">
        Product preview: the calcom/cal.com repository open in a Codebase Intel workspace. The question &ldquo;Where is booking
        confirmation handled?&rdquo; is answered with citations to the bookings confirm handler and to
        packages/features/bookings/lib/handleConfirmation.ts lines 42 to 118, drawn from 6 source files in 3 folders. A side
        panel shows a validated change to handleConfirmation.ts, with 7 lines added and 3 removed, ready to open as a pull
        request.
      </figcaption>

      <div aria-hidden="true" className="accent-glow pointer-events-none absolute -inset-x-6 -top-16 bottom-1/3 -z-10 opacity-90 blur-2xl" />

      <motion.div
        aria-hidden="true"
        variants={frameVariants}
        initial={reduceMotion ? false : 'hidden'}
        animate="show"
        className="rounded-[1.375rem] border border-line/80 bg-surface/60 p-1.5 shadow-[var(--shadow-lift)] sm:p-2"
      >
        <div className="overflow-hidden rounded-2xl border border-line bg-surface">
          <div className="flex h-10 items-center gap-3 border-b border-line bg-surface-2/70 px-3.5">
            <WindowDots />
            <div className="mx-auto flex min-w-0 items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 py-0.5 text-[11px] text-ink-3">
              <BrandGlyph className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">Codebase Intel · calcom/cal.com</span>
            </div>
            <div className="w-[46px] shrink-0" />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-[13rem_minmax(0,1fr)] lg:grid-cols-[13rem_minmax(0,1fr)_18.5rem]">
            <RepoSidebar reduceMotion={reduceMotion} />
            <ChatPanel />
            <DiffPanel />
          </div>
        </div>
      </motion.div>
    </figure>
  );
};

export default HeroMockup;
