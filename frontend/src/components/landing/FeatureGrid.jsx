import { ArrowDown, CircleDot, FileCode2, FolderTree, GitPullRequest, Layers, Network, ScanSearch, Search, Sparkles, TextSearch } from 'lucide-react';
import { CitationChip, DiffStat, MiniDiff } from './MockParts';
import Reveal from './Reveal';
import SectionHeading from './SectionHeading';
import { highlight } from './highlight';

/* ---------- Decorative mini visuals (aria-hidden by the card) ---------- */

const CITED_LINES = [
  { number: 11, code: '', cited: false },
  { number: 12, code: 'function getSession(req) {', cited: true },
  { number: 13, code: '  const sid = cookie(req);', cited: true },
  { number: 14, code: '  return verify(sid);', cited: true },
  { number: 15, code: '}', cited: false },
];

const CitationVisual = () => (
  <div className="space-y-2.5">
    <span className="block text-[13px]">
      <CitationChip dir="src/auth/" file="session.ts" lines="L12-L14" />
    </span>
    <div className="code-tokens overflow-hidden rounded-lg border border-code-line bg-code py-1.5 font-mono text-[10.5px] leading-[1.75]">
      {CITED_LINES.map((line) => (
        <div
          key={line.number}
          className={`flex whitespace-pre ${line.cited ? 'bg-highlight shadow-[inset_2px_0_0_var(--color-accent)]' : ''}`}
        >
          <span className={`w-7 shrink-0 select-none pr-2 text-right ${line.cited ? 'text-code-ink' : 'text-code-muted'}`}>{line.number}</span>
          <span className="min-w-0 overflow-hidden pr-1.5">{highlight(line.code)}</span>
        </div>
      ))}
    </div>
  </div>
);

const BUDGET_ROWS = [
  { paths: 'src/ · prisma/', status: 'Indexed first', tone: 'bg-success-soft text-success' },
  { paths: 'tests/ · fixtures/', status: 'Ranked later', tone: 'bg-warning-soft text-warning' },
  { paths: 'locales/de/ · dist/', status: 'Skipped', tone: 'bg-surface-3 text-ink-2' },
];

const BudgetVisual = () => (
  <ul className="space-y-1.5">
    {BUDGET_ROWS.map((row) => (
      <li key={row.paths} className="flex items-center justify-between gap-2 rounded-lg border border-line bg-surface px-2.5 py-1.5">
        <span className="min-w-0 truncate font-mono text-[11px] text-ink-2">{row.paths}</span>
        <span className={`shrink-0 rounded-full px-1.5 py-px text-[10px] font-medium ${row.tone}`}>{row.status}</span>
      </li>
    ))}
  </ul>
);

const SIGNALS = [
  { label: 'Semantic', icon: Sparkles },
  { label: 'Keyword', icon: TextSearch },
  { label: 'Path-aware', icon: FolderTree },
  { label: 'Import graph', icon: Network },
];

const RetrievalVisual = () => (
  <div>
    <div className="grid grid-cols-2 gap-1.5">
      {SIGNALS.map(({ label, icon: Icon }) => (
        <span key={label} className="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-2 py-1.5 text-[11px] font-medium text-ink-2">
          <Icon className="h-3 w-3 shrink-0 text-accent-strong" />
          <span className="truncate">{label}</span>
        </span>
      ))}
    </div>
    <div className="flex justify-center py-1 text-ink-3">
      <ArrowDown className="h-3.5 w-3.5" />
    </div>
    <div className="rounded-lg border border-accent/25 bg-accent-soft px-2.5 py-1.5 text-center text-[11px] font-medium text-accent-strong">
      One ranked set of evidence
    </div>
  </div>
);

const DIFF_LINES = [
  { type: 'ctx', code: '  const all = await list();' },
  { type: 'del', code: '  return all.length;' },
  { type: 'add', code: '  return open(all).length;' },
];

const DiffVisual = () => (
  <div className="overflow-hidden rounded-lg border border-code-line bg-code">
    <div className="flex items-center justify-between gap-2 border-b border-code-line bg-code-2 px-2.5 py-1.5 text-[10.5px]">
      <span className="truncate font-mono text-code-ink">src/count.ts</span>
      <span className="flex shrink-0 items-center gap-2">
        <DiffStat added={1} removed={1} />
        <span className="rounded border border-code-line px-1 text-code-muted">Unified</span>
      </span>
    </div>
    <MiniDiff lines={DIFF_LINES} />
  </div>
);

const PLAN_FILES = [
  { path: 'src/export/users.ts', role: 'edit' },
  { path: 'src/api/export.ts', role: 'call site' },
  { path: 'src/export/users.test.ts', role: 'test' },
];

const IssueVisual = () => (
  <div className="space-y-1.5">
    <div className="flex items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-1.5">
      <CircleDot className="h-3.5 w-3.5 shrink-0 text-success" />
      <span className="min-w-0 truncate font-mono text-[11px] text-ink-2">github.com/acme/web/issues/9</span>
    </div>
    <div className="rounded-lg border border-line bg-surface px-2.5 py-2">
      <p className="text-[10.5px] font-semibold uppercase tracking-[0.1em] text-ink-3">Plan · 3 files</p>
      <ul className="mt-1.5 space-y-1">
        {PLAN_FILES.map((file) => (
          <li key={file.path} className="flex items-center justify-between gap-2 text-[11px]">
            <span className="min-w-0 truncate font-mono text-ink-2">{file.path}</span>
            <span className="shrink-0 rounded bg-surface-2 px-1.5 text-[10px] text-ink-2">{file.role}</span>
          </li>
        ))}
      </ul>
    </div>
  </div>
);

const IMPORTERS = ['src/invoices/total.ts', 'src/cart/summary.tsx', 'src/emails/receipt.tsx'];

const ImpactVisual = () => (
  <div className="space-y-1.5">
    <div className="flex items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-1.5">
      <Search className="h-3.5 w-3.5 shrink-0 text-ink-3" />
      <span className="min-w-0 truncate font-mono text-[11px] text-ink">src/lib/formatCurrency.ts</span>
    </div>
    <div className="rounded-lg border border-line bg-surface px-2.5 py-2">
      <p className="text-[10.5px] font-semibold uppercase tracking-[0.1em] text-ink-3">Imported by 5 files</p>
      <ul className="mt-1.5 space-y-1">
        {IMPORTERS.map((path) => (
          <li key={path} className="truncate font-mono text-[11px] text-ink-2">{path}</li>
        ))}
        <li className="text-[11px] text-ink-3">+2 more</li>
      </ul>
    </div>
  </div>
);

/* ---------- Content ---------- */

const FEATURES = [
  {
    icon: FileCode2,
    title: 'Line-range citations',
    body: 'Answers cite code as path:L12-L40. Open the exact lines in the built-in viewer, or on GitHub at the indexed commit.',
    Visual: CitationVisual,
  },
  {
    icon: Layers,
    title: 'Monorepos up to 10,000 files',
    body: 'Instead of failing on size, files are ranked and the most important are indexed within a budget. The coverage report lists every omission and why.',
    Visual: BudgetVisual,
  },
  {
    icon: ScanSearch,
    title: 'Hybrid retrieval',
    body: 'File-level semantic search on NVIDIA Nemotron embeddings, keyword search, path-aware ranking and the import graph, working together.',
    Visual: RetrievalVisual,
  },
  {
    icon: GitPullRequest,
    title: 'Editable diff, real pull request',
    body: 'Review a GitHub-style green and red diff, unified or split, edit it if you need to, then open a pull request in one click.',
    Visual: DiffVisual,
  },
  {
    icon: CircleDot,
    title: 'Issue to pull request',
    body: 'Paste a GitHub issue URL. The agent plans the change across files, including the call sites and tests it touches.',
    Visual: IssueVisual,
  },
  {
    icon: Network,
    title: 'Impact analysis and code search',
    body: 'Instant code search, a repository overview with suggested questions, and every file that imports the one you plan to change.',
    Visual: ImpactVisual,
  },
];

const FeatureGrid = () => (
  <section id="features" aria-labelledby="features-title" className="scroll-mt-20 py-20 sm:py-28">
    <div className="content-shell">
      <Reveal>
        <SectionHeading
          id="features-title"
          eyebrow="Platform"
          title="Cited answers, checked references, real diffs"
          description="Read an unfamiliar repo by its line ranges, not by guesswork. Every file and symbol an answer names is checked against the index before you see it."
        />
      </Reveal>

      <ul className="mt-10 grid grid-cols-1 gap-3 sm:mt-14 sm:gap-4 md:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map(({ icon: Icon, title, body, Visual }, index) => (
          <Reveal
            as="li"
            key={title}
            delay={(index % 3) * 0.06}
            className="group flex flex-col rounded-2xl border border-line bg-surface p-5 shadow-[var(--shadow-soft)] transition-[border-color,box-shadow] duration-200 hover:border-line-strong hover:shadow-[var(--shadow-lift)] sm:p-6"
          >
            {/* Icon beside the title on phones (six stacked cards get long),
                above it from sm up. */}
            <div className="flex items-center gap-3 sm:block">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-strong">
                <Icon className="h-[18px] w-[18px]" aria-hidden="true" />
              </span>
              <h3 className="text-[15px] font-semibold tracking-tight text-ink sm:mt-4">{title}</h3>
            </div>
            <p className="mt-2 text-sm leading-relaxed text-ink-2 sm:mt-1.5">{body}</p>
            <div aria-hidden="true" className="mt-auto pt-4 sm:pt-5">
              <div className="rounded-xl border border-line bg-surface-2/60 p-3">
                <Visual />
              </div>
            </div>
          </Reveal>
        ))}
      </ul>
    </div>
  </section>
);

export default FeatureGrid;
