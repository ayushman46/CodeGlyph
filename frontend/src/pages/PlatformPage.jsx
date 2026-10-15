import {
  ArrowRight, Bot, Check, ChevronDown, ChevronRight, Cloud, Cog, Database, ExternalLink, FileCode2, FileDiff,
  FolderTree, GitBranch, GitPullRequest, Globe, KeyRound, Monitor, Network, Package, Route, Scissors, Search,
  Server, Sparkles, Timer,
} from 'lucide-react';
import { Fragment } from 'react';
import GoogleSignInButton from '../components/GoogleSignInButton';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import useStore from '../store/useStore';

const PAGE_SECTIONS = [
  { id: 'architecture', label: 'Architecture' },
  { id: 'indexing', label: 'Indexing' },
  { id: 'retrieval', label: 'Retrieval' },
  { id: 'answers', label: 'Answers' },
  { id: 'editing', label: 'Editing safety' },
  { id: 'deployment', label: 'Deployment' },
];

const LANES = [
  {
    id: 'ask',
    label: 'Ask',
    description: 'Questions stream back as cited answers.',
    summary: 'Your browser sends questions to the API, built with FastAPI, which retrieves evidence from Turso (libSQL): code sections, the full-text index and vectors.',
    nodes: [
      { icon: Monitor, title: 'Browser', meta: 'React app' },
      { icon: Server, title: 'API', meta: 'FastAPI' },
      { icon: Database, title: 'Turso / libSQL', meta: 'chunks · FTS · vectors', tone: 'store', wide: true },
    ],
  },
  {
    id: 'index',
    label: 'Index',
    description: 'Runs in the background and writes to Turso.',
    summary: 'The ingestion worker downloads the GitHub archive, the chunker splits files into sections, and NVIDIA embeddings are computed; everything is stored in Turso.',
    nodes: [
      { icon: Cog, title: 'Ingestion worker', meta: 'durable jobs' },
      { icon: Package, title: 'GitHub archive', meta: 'streamed snapshot' },
      { icon: Scissors, title: 'Chunker', meta: 'paths · symbols · lines' },
      { icon: Sparkles, title: 'NVIDIA embeddings', meta: 'Nemotron' },
    ],
  },
  {
    id: 'change',
    label: 'Change',
    description: 'Edits are validated before you see them.',
    summary: 'The code editor agent reads the current raw files from GitHub and produces a validated diff for you to review; a pull request is opened only when you choose to.',
    nodes: [
      { icon: Bot, title: 'Code editor agent', meta: 'plans across files' },
      { icon: FileCode2, title: 'GitHub raw files', meta: 'current contents' },
      { icon: FileDiff, title: 'Validated diff', meta: 'you review & edit' },
      { icon: GitPullRequest, title: 'Your PR', meta: 'branch or fork', tone: 'accent' },
    ],
  },
];

const PIPELINE = [
  ['Fetch', 'The ingestion worker streams the repository archive from GitHub and records the commit it indexed.'],
  ['Rank and select', 'Files are ranked by importance (source, schema and config first; tests, fixtures, generated code and data later) and selected within the budget: up to 10,000 files, 100 MB and 15,000 sections per repository by default.'],
  ['Skip and report', 'Non-English translation catalogues, build artifacts, dependency folders, lockfiles and binaries are skipped. Every omission is recorded with its reason in the coverage report.'],
  ['Chunk', 'Files are split into source-aware sections that keep their path, symbols and exact line ranges.'],
  ['Keyword index first', 'Sections are written with a full-text index, so keyword search and questions work within moments.'],
  ['Semantic index next', 'NVIDIA Nemotron embeddings are computed in the background and stored as vectors, with live progress.'],
  ['Refresh incrementally', 'Re-indexing hashes every file and only re-processes what changed. The previous index stays queryable until the new one is ready.'],
];

const SIGNALS = [
  { icon: Sparkles, title: 'File-level semantic search', text: 'Ranks whole files by meaning using their embeddings, then picks the best sections inside them.' },
  { icon: Search, title: 'Keyword (FTS) search', text: 'Full-text search finds exact identifiers, error messages and strings that embeddings can blur.' },
  { icon: Route, title: 'Path-aware ranking', text: 'A file you name is read directly, and questions about a specific area favour files whose paths match it.' },
  { icon: Network, title: 'Import graph', text: 'For impact questions and issue-driven edits, files that import the matched code are added from the resolved graph.' },
];

const SAFETY = [
  'Plans the change across files, including call sites and tests.',
  'Reads the complete current files from GitHub, falling back to the indexed source only if GitHub is unreachable.',
  'Writes exact search/replace hunks. Each must match its file exactly once; anything that fails validation is never shown as a patch.',
  'Shows a GitHub-style diff, unified or split, that you can edit before pushing.',
  'Requires a short-lived, file-scoped ticket for every pull request action.',
  'Opens the PR from a new branch in your repository, or from a fork when you lack write access. You can also download a .patch.',
  'Treats repository content, comments and issues as untrusted input: the agent is instructed never to act on instructions found inside them.',
];

const DEPLOYMENT = [
  { icon: Globe, title: 'Frontend', text: 'A static Vite and React build. Host it on a CDN such as Vercel, or let the API serve it.' },
  { icon: Server, title: 'API and worker', text: 'FastAPI, with the ingestion worker running in the same process by default. Indexing runs in the background, so it needs a persistent service such as Render.' },
  { icon: Database, title: 'Database', text: 'Turso (libSQL) stores sections, the full-text index, vectors, repositories and conversations. Indexing jobs are durable and leased, so interrupted work is recovered.' },
  { icon: Cloud, title: 'Services', text: 'Supabase Auth for Google sign-in, the NVIDIA API for Nemotron embeddings and models, Razorpay for Team payments, and a GitHub OAuth app for pull requests.' },
  { icon: Timer, title: 'Cold starts', text: 'On a free tier the API may sleep when idle. The first request after a quiet period can take up to a minute, and the app shows a notice while it wakes.' },
  { icon: KeyRound, title: 'Secrets stay server-side', text: 'Provider keys and the GitHub token encryption key live only on the API. The browser receives only public keys.' },
];

const SectionHeading = ({ id, eyebrow, title, children }) => (
  <div className="max-w-2xl">
    <p className="eyebrow">{eyebrow}</p>
    <h2 id={id} className="section-title mt-3 text-balance text-3xl text-ink sm:text-4xl">{title}</h2>
    {children && <p className="mt-4 text-base leading-relaxed text-ink-2 sm:text-[17px]">{children}</p>}
  </div>
);

/* ------------------------------------------------------------- diagram */

const Connector = ({ index }) => (
  <div className="relative h-6 lg:h-auto" aria-hidden="true">
    <div className="absolute inset-0 overflow-hidden">
      <span className="absolute bottom-1 left-1/2 top-0 w-px -translate-x-1/2 bg-line-strong lg:bottom-auto lg:left-0 lg:right-1.5 lg:top-1/2 lg:h-px lg:w-auto lg:translate-x-0 lg:-translate-y-1/2" />
      {/* A data pulse travels along desktop connectors; it rests off-screen
          (left: 100%) whenever the animation is not running. */}
      <span className="absolute inset-0 hidden lg:block">
        <span
          className="absolute top-1/2 block h-1.5 w-1.5 -translate-y-1/2 rounded-full bg-accent shadow-[0_0_8px_var(--color-accent)] motion-reduce:hidden"
          style={{ left: '100%', animation: 'ci-indeterminate 2.8s ease-in-out infinite', animationDelay: `${index * 0.45}s` }}
        />
      </span>
    </div>
    <ChevronDown className="absolute -bottom-1 left-1/2 h-3.5 w-3.5 -translate-x-1/2 text-ink-3 lg:hidden" />
    <ChevronRight className="absolute right-0 top-1/2 hidden h-3.5 w-3.5 -translate-y-1/2 translate-x-1 text-ink-3 lg:block" />
  </div>
);

const DiagramNode = ({ icon: Icon, title, meta, tone, wide }) => {
  const toneClass = tone === 'store'
    ? 'border-accent/35 bg-surface ring-1 ring-accent/10'
    : tone === 'accent'
      ? 'border-accent/40 bg-accent-soft'
      : 'border-line bg-surface';
  return (
    <div className={`relative flex min-w-0 items-center gap-3 rounded-2xl border px-3.5 py-3 shadow-[var(--shadow-soft)] lg:flex-col lg:items-start lg:gap-3 lg:p-4 ${toneClass} ${wide ? 'lg:col-span-3' : ''}`}>
      <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${tone ? 'bg-accent text-accent-ink' : 'border border-line bg-surface-2 text-ink-2'}`}>
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <p className="text-[13.5px] font-semibold leading-snug text-ink">{title}</p>
        <p className={`mt-0.5 font-mono text-[11px] leading-snug ${tone === 'accent' ? 'text-ink-2' : 'text-ink-3'}`}>{meta}</p>
      </div>
    </div>
  );
};

const ArchitectureDiagram = () => (
  <figure className="relative mt-10 overflow-hidden rounded-3xl border border-line bg-surface-2/60 shadow-[var(--shadow-soft)]">
    <div className="bg-grid pointer-events-none absolute inset-0 opacity-80" aria-hidden="true" />
    <div className="relative divide-y divide-line px-4 sm:px-6 lg:px-8">
      {LANES.map((lane, laneIndex) => (
        <div key={lane.id} className="grid gap-4 py-6 lg:grid-cols-[10.5rem_minmax(0,1fr)] lg:items-center lg:gap-8 lg:py-8">
          <div className="flex items-start gap-3 lg:block">
            <span className="mt-0.5 font-mono text-[11px] font-semibold text-accent-strong lg:mt-0">0{laneIndex + 1}</span>
            <div className="min-w-0 lg:mt-1.5">
              <h3 className="text-[15px] font-semibold text-ink">{lane.label}</h3>
              <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">{lane.description}</p>
            </div>
          </div>
          <p className="sr-only">{lane.summary}</p>
          <div
            className="flex flex-col lg:grid lg:grid-cols-[minmax(0,1fr)_2rem_minmax(0,1fr)_2rem_minmax(0,1fr)_2rem_minmax(0,1fr)] lg:items-stretch"
            aria-hidden="true"
          >
            {lane.nodes.map((node, index) => (
              <Fragment key={node.title}>
                {index > 0 && <Connector index={laneIndex * 3 + index} />}
                <DiagramNode {...node} />
              </Fragment>
            ))}
          </div>
        </div>
      ))}
    </div>
    <figcaption className="relative border-t border-line bg-surface/70 px-4 py-3 text-xs leading-relaxed text-ink-2 sm:px-6 lg:px-8">
      Three flows share one API and one database. Arrows show the direction data moves.
    </figcaption>
  </figure>
);

/* ------------------------------------------------------------- mockups */

const CODE_LINES = [
  [12, [['keyword', 'export async function '], ['function', 'requireSession'], ['punctuation', '(req) {']]],
  [13, [['plain', '  const token = '], ['function', 'readCookie'], ['punctuation', '(req, '], ['string', "'session'"], ['punctuation', ');']]],
  [14, [['keyword', '  if '], ['punctuation', '(!token) '], ['keyword', 'throw new '], ['class-name', 'AuthError'], ['punctuation', '('], ['number', '401'], ['punctuation', ');']]],
  [15, [['keyword', '  return '], ['function', 'verifySignedToken'], ['punctuation', '(token);']]],
  [16, [['punctuation', '}']]],
];

const AnswerMockup = () => (
  <div className="relative min-w-0" role="img" aria-label="Example: a streaming answer citing src/server/auth.ts lines 12 to 16, with the cited lines open in the file viewer.">
    <div className="rounded-2xl border border-line bg-surface p-4 shadow-[var(--shadow-lift)] sm:p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">Security review</Badge>
        <span className="flex items-center gap-1.5 text-[11px] text-ink-3">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success motion-reduce:animate-none" />
          Streaming
        </span>
        <span className="ml-auto text-[11px] text-ink-3">Example</span>
      </div>
      <p className="mt-4 text-[13px] font-medium text-ink">Where are sessions validated?</p>
      <p className="mt-2 text-[13px] leading-relaxed text-ink-2">
        Every protected route calls <code className="rounded border border-line bg-surface-2 px-1 font-mono text-[11.5px] text-ink">requireSession</code>,
        which reads the session cookie and verifies its signature before the handler runs
        <span className="ml-1 whitespace-nowrap rounded bg-accent-soft px-1 font-mono text-[11px] text-accent-strong">auth.ts:L12-L16</span>.
        <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-blink bg-accent motion-reduce:animate-none" />
      </p>
      <div className="mt-4 border-t border-line pt-3">
        <p className="text-[11px] font-medium text-ink-3">Sources by folder</p>
        <div className="mt-2 space-y-1.5 font-mono text-[11.5px]">
          <div className="flex min-w-0 items-center gap-2">
            <FolderTree className="h-3.5 w-3.5 shrink-0 text-ink-3" />
            <span className="text-ink-3">src/server/</span>
            <span className="truncate rounded-md bg-surface-2 px-1.5 py-0.5 text-ink">auth.ts:L12-L16</span>
          </div>
          <div className="flex min-w-0 items-center gap-2">
            <FolderTree className="h-3.5 w-3.5 shrink-0 text-ink-3" />
            <span className="text-ink-3">src/lib/</span>
            <span className="truncate rounded-md bg-surface-2 px-1.5 py-0.5 text-ink">cookies.ts:L4-L19</span>
          </div>
        </div>
      </div>
    </div>

    <div className="relative mt-3 overflow-hidden rounded-2xl border border-code-line bg-code shadow-[var(--shadow-lift)] sm:ml-10 lg:-mr-4">
      <div className="flex items-center gap-2 border-b border-code-line bg-code-2 px-3.5 py-2.5 font-mono text-[11px]">
        <FileCode2 className="h-3.5 w-3.5 shrink-0 text-code-muted" />
        <span className="min-w-0 truncate text-code-ink">src/server/auth.ts</span>
        <span className="hidden text-code-muted sm:inline">@ 4f2c9e1</span>
        <span className="ml-auto flex shrink-0 items-center gap-1 text-code-muted">GitHub <ExternalLink className="h-3 w-3" /></span>
      </div>
      {/* Long lines wrap beside a fixed gutter (no scroll region inside the illustration). */}
      <pre className="code-tokens py-2 font-mono text-[11.5px] leading-6">
        {CODE_LINES.map(([line, tokens]) => (
          <div key={line} className="flex border-l-2 border-accent bg-highlight pr-4">
            <span className="w-10 shrink-0 select-none pr-3 text-right text-code-ink/60">{line}</span>
            <span className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">
              {tokens.map(([kind, text], index) => (
                <span key={index} className={kind === 'plain' ? '' : `token ${kind}`}>{text}</span>
              ))}
            </span>
          </div>
        ))}
      </pre>
    </div>
  </div>
);

const DIFF_LINES = [
  ['context', 8, 8, "  const raw = req.headers.get('cookie') ?? '';"],
  ['del', 9, null, '  return parse(raw)[name];'],
  ['add', null, 9, '  const value = parse(raw)[name];'],
  ['add', null, 10, '  return value ? decodeURIComponent(value) : null;'],
  ['context', 10, 11, '}'],
];

const DiffMockup = () => (
  <div className="overflow-hidden rounded-2xl border border-code-line bg-code shadow-[var(--shadow-lift)]" role="img" aria-label="Example: a validated diff in src/lib/cookies.ts that replaces one line with two, ready to review, edit, download as a patch, or open as a pull request.">
    <div className="flex flex-wrap items-center gap-2 border-b border-code-line bg-code-2 px-3.5 py-2.5">
      <FileDiff className="h-3.5 w-3.5 text-code-muted" />
      <span className="min-w-0 truncate font-mono text-[11px] text-code-ink">src/lib/cookies.ts</span>
      <span className="font-mono text-[11px] text-diff-add-ink">+2</span>
      <span className="font-mono text-[11px] text-diff-del-ink">−1</span>
      <span className="ml-auto flex rounded-md border border-code-line p-0.5 text-[10.5px]">
        <span className="rounded bg-code-line px-1.5 py-0.5 text-code-ink">Unified</span>
        <span className="px-1.5 py-0.5 text-code-muted">Split</span>
      </span>
    </div>
    <div className="py-2 font-mono text-[11.5px] leading-6">
      {DIFF_LINES.map(([kind, oldLine, newLine, text]) => (
        <div
          key={`${kind}-${oldLine}-${newLine}`}
          className={`flex pr-4 ${kind === 'add' ? 'bg-diff-add text-diff-add-ink' : kind === 'del' ? 'bg-diff-del text-diff-del-ink' : 'text-code-ink'}`}
        >
          <span className="w-9 shrink-0 select-none pr-2 text-right text-code-ink/60">{oldLine ?? ''}</span>
          <span className="w-9 shrink-0 select-none pr-2 text-right text-code-ink/60">{newLine ?? ''}</span>
          <span className="w-5 shrink-0 select-none text-center">{kind === 'add' ? '+' : kind === 'del' ? '−' : ''}</span>
          <span className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{text}</span>
        </div>
      ))}
    </div>
    <div className="flex flex-wrap items-center gap-2 border-t border-code-line bg-code-2 px-3.5 py-3">
      <span className="flex items-center gap-1.5 text-[11px] text-diff-add-ink">
        <Check className="h-3.5 w-3.5" /> 1 hunk validated
      </span>
      <span className="ml-auto flex items-center gap-2">
        <span className="whitespace-nowrap rounded-md border border-code-line px-2 py-1 text-[11px] text-code-ink">Download .patch</span>
        <span className="flex items-center gap-1 whitespace-nowrap rounded-md bg-accent px-2 py-1 text-[11px] font-medium text-accent-ink">
          <GitBranch className="h-3 w-3" /> Open pull request
        </span>
      </span>
    </div>
  </div>
);

/* ---------------------------------------------------------------- page */

const PlatformPage = () => {
  const user = useStore((state) => state.user);

  // overflow-x-clip (not overflow-hidden) trims decorative overhang without
  // creating a scroll container, so the lg:sticky columns below still stick.
  return (
    <main className="relative overflow-x-clip pb-24">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-[36rem]" aria-hidden="true">
        <div className="bg-grid absolute inset-0 opacity-70" />
        <div className="accent-glow absolute inset-0" />
      </div>

      {/* Hero */}
      <section className="content-shell relative pt-16 sm:pt-24" aria-labelledby="platform-title">
        <div className="mx-auto max-w-3xl text-center">
          <p className="eyebrow animate-fade-in">Platform</p>
          <h1 id="platform-title" className="display-title mt-4 animate-rise text-balance text-[2.5rem] text-ink sm:text-6xl">
            {/* An accent-to-accent-strong gradient keeps large-text contrast above 3:1 in both themes. */}
            How Codebase Intel{' '}
            <span className="bg-linear-to-r from-accent to-accent-strong bg-clip-text text-transparent">works</span>.
          </h1>
          <p className="mx-auto mt-5 max-w-2xl animate-rise text-base leading-relaxed text-ink-2 sm:text-lg">
            How a public repository becomes a searchable index, how each answer is tied to file:line citations
            checked against that index, and how a described change becomes a diff you review before it ships as a pull request.
          </p>
        </div>
        <nav aria-label="On this page" className="mx-auto mt-8 flex max-w-3xl flex-wrap justify-center gap-2">
          {PAGE_SECTIONS.map((section) => (
            <a
              key={section.id}
              href={`#${section.id}`}
              className="rounded-full border border-line bg-surface/80 px-3.5 py-1.5 text-[13px] font-medium text-ink-2 backdrop-blur transition-colors hover:border-line-strong hover:text-ink"
            >
              {section.label}
            </a>
          ))}
        </nav>
      </section>

      {/* Architecture */}
      <section id="architecture" className="content-shell relative mt-16 scroll-mt-20 sm:mt-24" aria-labelledby="architecture-title">
        <SectionHeading id="architecture-title" eyebrow="Architecture" title="One API, one database, three flows.">
          Questions, indexing and code changes all pass through the same FastAPI service and the same Turso
          database, but each follows its own path.
        </SectionHeading>
        <ArchitectureDiagram />
      </section>

      {/* Indexing */}
      <section id="indexing" className="content-shell relative mt-24 scroll-mt-20 sm:mt-32" aria-labelledby="indexing-title">
        <div className="grid grid-cols-1 gap-10 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:gap-16">
          <div className="min-w-0 lg:sticky lg:top-24 lg:self-start">
            <SectionHeading id="indexing-title" eyebrow="Indexing pipeline" title="Large repositories get indexed, not rejected.">
              Instead of failing on size, the pipeline indexes the most important files within a budget, makes keyword
              search available first and builds the semantic index in the background.
            </SectionHeading>
            <dl className="mt-8 grid max-w-md grid-cols-3 overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-soft)]">
              {[['10,000', 'files'], ['100 MB', 'source'], ['15,000', 'sections']].map(([value, label], index) => (
                <div key={label} className={`flex flex-col-reverse px-3 py-4 text-center ${index > 0 ? 'border-l border-line' : ''}`}>
                  <dt className="mt-1 text-xs text-ink-3">{label}</dt>
                  <dd className="display-title text-xl text-ink sm:text-2xl">{value}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-xs text-ink-2">Default budget per repository.</p>
          </div>
          <ol className="relative min-w-0">
            {PIPELINE.map(([title, text], index) => (
              <li key={title} className="relative grid grid-cols-[2.25rem_minmax(0,1fr)] gap-4 pb-8 last:pb-0">
                {index < PIPELINE.length - 1 && (
                  <span className="absolute bottom-0 left-[1.125rem] top-10 w-px -translate-x-1/2 bg-line" aria-hidden="true" />
                )}
                <span className="relative flex h-9 w-9 items-center justify-center rounded-full border border-line bg-surface font-mono text-[12px] font-semibold text-accent-strong shadow-[var(--shadow-soft)]">
                  {String(index + 1).padStart(2, '0')}
                </span>
                <div className="min-w-0 pt-1.5">
                  <h3 className="text-[15px] font-semibold text-ink">{title}</h3>
                  <p className="mt-1.5 text-[15px] leading-relaxed text-ink-2">{text}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* Retrieval */}
      <section id="retrieval" className="content-shell relative mt-24 scroll-mt-20 sm:mt-32" aria-labelledby="retrieval-title">
        <SectionHeading id="retrieval-title" eyebrow="Retrieval" title="Four signals, one bounded set of evidence.">
          Each question combines several retrieval methods. The answer model receives a bounded selection of the
          strongest evidence, never the whole repository.
        </SectionHeading>
        <ul className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {SIGNALS.map(({ icon: Icon, title, text }) => (
            <li key={title} className="group relative overflow-hidden rounded-2xl border border-line bg-surface p-5 shadow-[var(--shadow-soft)] transition-shadow duration-200 hover:shadow-[var(--shadow-lift)]">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-accent-soft text-accent-strong">
                <Icon className="h-4 w-4" aria-hidden="true" />
              </span>
              <h3 className="mt-4 text-[15px] font-semibold text-ink">{title}</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-ink-3">{text}</p>
            </li>
          ))}
        </ul>
        <p className="mt-5 max-w-3xl text-[13px] leading-relaxed text-ink-2">
          README-style files are used for repository-wide overview questions, or when you ask about documentation,
          rather than as evidence for implementation questions.
        </p>
      </section>

      {/* Answers & citations */}
      <section id="answers" className="content-shell relative mt-24 scroll-mt-20 sm:mt-32" aria-labelledby="answers-title">
        {/* grid-cols-1 is minmax(0, 1fr): without it the mobile column grows to the code lines' width. */}
        <div className="grid grid-cols-1 items-center gap-12 lg:grid-cols-2 lg:gap-16">
          <div className="min-w-0">
            <SectionHeading id="answers-title" eyebrow="Answers and citations" title="Every answer shows its sources.">
              Answers stream in real time and cite code as a path and line range, so each claim can be checked
              against the exact lines it came from.
            </SectionHeading>
            <ul className="mt-8 space-y-4">
              {[
                ['Exact citations', <>Code is cited as <code className="whitespace-nowrap font-mono text-[0.9em] text-ink">path:L12-L40</code>.</>],
                ['Built-in file viewer', 'Every citation opens the exact lines, with a link to GitHub at the indexed commit.'],
                ['Sources by folder', 'See at a glance which parts of the repository an answer relied on.'],
                ['Follow-ups in context', 'Recent messages stay in context, and conversations are saved per repository.'],
              ].map(([title, text]) => (
                <li key={title} className="flex gap-3">
                  <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent-soft text-accent-strong">
                    <Check className="h-3 w-3" strokeWidth={3} aria-hidden="true" />
                  </span>
                  <p className="text-[15px] leading-relaxed text-ink-2">
                    <span className="font-semibold text-ink">{title}.</span> {text}
                  </p>
                </li>
              ))}
            </ul>
          </div>
          <AnswerMockup />
        </div>
      </section>

      {/* Editing safety */}
      <section id="editing" className="content-shell relative mt-24 scroll-mt-20 sm:mt-32" aria-labelledby="editing-title">
        <div className="grid grid-cols-1 items-start gap-12 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)] lg:gap-16">
          <div className="min-w-0 lg:order-2">
            <SectionHeading id="editing-title" eyebrow="Editing safety" title="Nothing is pushed without your review.">
              Describe a change or paste a GitHub issue URL. The code editor agent proposes it as a diff, and a pull
              request exists only after you open it.
            </SectionHeading>
            <ul className="mt-8 space-y-3">
              {SAFETY.map((item) => (
                <li key={item} className="flex gap-3 text-[15px] leading-relaxed text-ink-2">
                  <Check className="mt-1 h-4 w-4 shrink-0 text-success" aria-hidden="true" />
                  <span className="min-w-0">{item}</span>
                </li>
              ))}
            </ul>
            <p className="mt-6 text-[13px] leading-relaxed text-ink-2">
              The pull request is created as a single commit, after the server checks that each file still matches the
              version you reviewed.
            </p>
          </div>
          <div className="min-w-0 lg:order-1 lg:sticky lg:top-24">
            <DiffMockup />
          </div>
        </div>
      </section>

      {/* Deployment */}
      <section id="deployment" className="content-shell relative mt-24 scroll-mt-20 sm:mt-32" aria-labelledby="deployment-title">
        <SectionHeading id="deployment-title" eyebrow="Deployment notes" title="Static frontend, persistent backend.">
          The frontend is a static site; the API and worker need a service that stays running while repositories index.
        </SectionHeading>
        <ul className="mt-10 grid gap-px overflow-hidden rounded-3xl border border-line bg-line sm:grid-cols-2 lg:grid-cols-3">
          {DEPLOYMENT.map(({ icon: Icon, title, text }) => (
            <li key={title} className="bg-surface p-6">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-line bg-surface-2 text-ink-2">
                <Icon className="h-4 w-4" aria-hidden="true" />
              </span>
              <h3 className="mt-4 text-[15px] font-semibold text-ink">{title}</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-ink-3">{text}</p>
            </li>
          ))}
        </ul>
      </section>

      {/* Closing call to action */}
      <section className="content-shell relative mt-24 sm:mt-32" aria-labelledby="platform-cta-title">
        <div className="relative overflow-hidden rounded-3xl border border-line bg-surface px-6 py-12 text-center shadow-[var(--shadow-soft)] sm:px-12 sm:py-16">
          <div className="accent-glow pointer-events-none absolute inset-0 opacity-80" aria-hidden="true" />
          <div className="relative">
            <h2 id="platform-cta-title" className="section-title mx-auto max-w-xl text-balance text-3xl text-ink sm:text-4xl">
              Test it on code you already know.
            </h2>
            <p className="mx-auto mt-4 max-w-lg text-base leading-relaxed text-ink-2">
              Paste a public GitHub URL or owner/repo. Ask your first question once keyword search is ready; semantic search keeps building in the background.
            </p>
            <div className="mt-8 flex flex-col items-stretch justify-center gap-3 sm:flex-row sm:items-center">
              {user ? (
                <Button to="/app" variant="dark" size="lg" iconRight={ArrowRight}>Open workspace</Button>
              ) : (
                <GoogleSignInButton size="lg" label="Start free with Google" />
              )}
              <Button to="/docs" variant="ghost" size="lg">Read the docs</Button>
            </div>
          </div>
        </div>
      </section>
    </main>
  );
};

export default PlatformPage;
