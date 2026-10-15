import { Check, Download, Github, GitPullRequest, ListChecks, Loader2 } from 'lucide-react';
import { CitationChip, DiffStat, MiniDiff } from './MockParts';
import Reveal from './Reveal';
import SectionHeading from './SectionHeading';

/* ---------- Mini UI snippets (aria-hidden by the step card) ---------- */

const IndexSnippet = () => (
  <div className="space-y-2.5">
    <div className="flex items-center gap-2 rounded-lg border border-line bg-surface py-1.5 pl-2.5 pr-1.5 shadow-sm">
      <Github className="h-3.5 w-3.5 shrink-0 text-ink-3" />
      <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink">calcom/cal.com</span>
      <span className="shrink-0 rounded-md bg-accent px-2 py-0.5 text-[11px] font-semibold text-accent-ink">Index</span>
    </div>
    <div className="space-y-2.5 rounded-lg border border-line bg-surface p-2.5 text-[11.5px]">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-ink-2">
          <Check className="h-3.5 w-3.5 text-success" />
          Keyword search
        </span>
        <span className="font-medium text-success">Ready</span>
      </div>
      <div>
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1.5 text-ink-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
            Semantic index
          </span>
          <span className="font-mono tabular-nums text-ink-3">72%</span>
        </div>
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-surface-3">
          <div className="h-full w-[72%] rounded-full bg-accent" />
        </div>
      </div>
      <div className="flex items-start gap-1.5">
        <ListChecks className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" />
        <div className="min-w-0">
          <p className="text-ink-2">Coverage report</p>
          <p className="mt-0.5 text-[10.5px] text-ink-3">Omissions listed with reasons</p>
        </div>
      </div>
    </div>
  </div>
);

const WORKFLOWS = ['Onboarding', 'Security review', 'Due diligence'];

const AskSnippet = () => (
  <div className="space-y-2.5">
    <div className="flex flex-wrap gap-1.5">
      {WORKFLOWS.map((workflow, index) => (
        <span
          key={workflow}
          className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${index === 0 ? 'border-accent/30 bg-accent-soft text-accent-strong' : 'border-line bg-surface text-ink-2'}`}
        >
          {workflow}
        </span>
      ))}
      <span className="rounded-full border border-dashed border-line-strong px-2 py-0.5 text-[11px] text-ink-2">+3 more</span>
    </div>
    <div className="rounded-lg border border-line bg-surface p-2.5 text-[12px] leading-relaxed text-ink-2">
      <p className="font-medium text-ink">Where should a new engineer start?</p>
      <p className="mt-1">
        Begin with the booking flow{' '}
        <CitationChip file="handleNewBooking.ts" lines="L1-L60" />
      </p>
      <p className="mt-1.5 text-[11px] text-ink-3">Sources · 4 files in 2 folders</p>
    </div>
  </div>
);

const SHIP_LINES = [
  { type: 'del', code: 'await notify(evt);' },
  { type: 'add', code: 'if (isAccepted(booking)) return;' },
  { type: 'add', code: 'await markAccepted(booking.id);' },
];

const ShipSnippet = () => (
  <div className="space-y-2.5">
    <div className="overflow-hidden rounded-lg border border-code-line bg-code">
      <div className="flex items-center justify-between gap-2 border-b border-code-line bg-code-2 px-2.5 py-1.5 text-[10.5px]">
        <span className="truncate text-code-ink">2 files changed</span>
        <DiffStat added={12} removed={3} />
      </div>
      <MiniDiff lines={SHIP_LINES} size="sm" />
    </div>
    <div className="flex flex-wrap gap-1.5">
      <span className="inline-flex items-center gap-1 rounded-lg bg-accent px-2 py-1 text-[11.5px] font-medium text-accent-ink">
        <GitPullRequest className="h-3.5 w-3.5" />
        Create pull request
      </span>
      <span className="inline-flex items-center gap-1 rounded-lg border border-line bg-surface px-2 py-1 text-[11.5px] font-medium text-ink-2">
        <Download className="h-3.5 w-3.5" />
        .patch
      </span>
    </div>
  </div>
);

/* ---------- Content ---------- */

const STEPS = [
  {
    title: 'Index',
    body: 'Paste a public GitHub URL or owner/repo. Keyword search is ready within moments while the semantic index builds in the background, with live progress.',
    Snippet: IndexSnippet,
  },
  {
    title: 'Ask',
    body: 'Pick a workflow, from new-engineer onboarding to security review. Answers stream in with path:line citations and sources grouped by folder.',
    Snippet: AskSnippet,
  },
  {
    title: 'Ship a PR',
    body: 'Describe a change or paste an issue URL. Review the validated diff, edit it, then open a pull request or download a .patch.',
    Snippet: ShipSnippet,
  },
];

const HowItWorks = () => (
  <section id="how-it-works" aria-labelledby="how-title" className="scroll-mt-20 border-y border-line bg-surface-2/50 py-20 sm:py-28">
    <div className="content-shell">
      <Reveal>
        <SectionHeading
          id="how-title"
          eyebrow="How it works"
          title="Repo URL to pull request in three steps"
          description="Keyword search is ready in moments. Semantic search builds in the background. You review every line before it ships."
        />
      </Reveal>

      <ol className="mt-10 grid grid-cols-1 gap-3 sm:mt-14 sm:gap-4 lg:grid-cols-3">
        {STEPS.map(({ title, body, Snippet }, index) => (
          <Reveal
            as="li"
            key={title}
            delay={index * 0.08}
            className="flex flex-col rounded-2xl border border-line bg-surface p-5 shadow-[var(--shadow-soft)] sm:p-6"
          >
            <div aria-hidden="true" className="rounded-xl border border-line bg-surface-2/60 p-4 lg:flex lg:min-h-[14.5rem] lg:flex-col lg:justify-center">
              <Snippet />
            </div>
            <div className="mt-5 flex items-center gap-3">
              <span aria-hidden="true" className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-line bg-surface-2 font-mono text-[12px] font-semibold text-ink-2">
                {index + 1}
              </span>
              <h3 className="text-[15px] font-semibold tracking-tight text-ink">{title}</h3>
            </div>
            <p className="mt-2 text-sm leading-relaxed text-ink-2">{body}</p>
          </Reveal>
        ))}
      </ol>
    </div>
  </section>
);

export default HowItWorks;
