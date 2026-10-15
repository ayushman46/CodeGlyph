import {
  ArrowLeft, ArrowRight, BookOpen, Check, Compass, Copy, GitPullRequest, Keyboard, Layers, Lightbulb, Link2,
  MessageSquareText, Rocket, Search, ShieldCheck,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import GoogleSignInButton from '../components/GoogleSignInButton';
import Button from '../components/ui/Button';
import Kbd from '../components/ui/Kbd';
import useStore from '../store/useStore';

/*
 * Content blocks. Each section is a list of blocks rendered in order. Text
 * may mark code with backticks (`node_modules`), rendered as inline code.
 *   { type: 'p', text }                       paragraph
 *   { type: 'h2', text }                      sub-heading (listed under "On this page")
 *   { type: 'steps', items: [[title, text, pills?]] }
 *   { type: 'list', items: [[title, text]] }
 *   { type: 'callout', title, text }
 *   { type: 'pills', label, items }           small code examples
 *   { type: 'stats', items: [[value, label]] }
 *   { type: 'cards' }                         introduction quick links
 *   { type: 'shortcuts', items: [[keys, description]] }
 */
const SECTIONS = [
  {
    id: 'introduction',
    label: 'Introduction',
    icon: BookOpen,
    eyebrow: 'Documentation',
    title: 'Understand the code before you change it.',
    summary: 'Codebase Intel indexes a public GitHub repository, answers questions about it with exact file and line citations, and helps you turn a change into a reviewed pull request.',
    blocks: [
      { type: 'p', text: 'The first hours in an unfamiliar repository are usually spent guessing where things live. Codebase Intel keeps every answer connected to the source that supports it, so you can move from an explanation straight to the lines of code behind it.' },
      { type: 'pills', label: 'A citation looks like this', items: ['src/server/auth.ts:L12-L40'] },
      { type: 'p', text: 'This guide covers the whole workflow: adding a repository, how large repositories are indexed, asking questions, searching and analysing impact, and proposing changes as pull requests. Every heading has a link you can share.' },
      { type: 'h2', text: 'Where to go next' },
      { type: 'cards' },
    ],
  },
  {
    id: 'getting-started',
    label: 'Getting started',
    icon: Rocket,
    eyebrow: 'Getting started',
    title: 'Add a repository and ask your first question.',
    summary: 'Sign in with Google, paste a repository, and start asking once keyword search is ready. Semantic search keeps building in the background.',
    blocks: [
      {
        type: 'steps',
        items: [
          ['Sign in with Google', 'Your workspace, repositories and conversations are tied to your Google account and visible only to you.'],
          ['Add a repository', 'Paste a public GitHub URL or the short `owner/repo` form. Both of these add the same repository:', ['https://github.com/calcom/cal.com', 'calcom/cal.com']],
          ['Watch it index', 'Keyword search is ready within moments. The semantic index builds in the background, with live progress, and you can start asking as soon as the repository is searchable.'],
          ['Ask a question', 'Pick a workflow, ask in plain language, and follow the citations into the code.'],
        ],
      },
      { type: 'callout', title: 'Public repositories only', text: 'Only public GitHub repositories can be indexed today, up to 30 per account. Your plan limit counts the indexed source across all of your repositories: 200 MB on Explorer, 800 MB on Team.' },
    ],
  },
  {
    id: 'indexing',
    label: 'Indexing & large repositories',
    icon: Layers,
    eyebrow: 'Indexing',
    title: 'Large repositories are indexed, not rejected.',
    summary: 'Files are ranked by importance and the most important ones are indexed within a budget, with every omission explained.',
    blocks: [
      { type: 'h2', text: 'The importance budget' },
      { type: 'p', text: 'Every file is ranked by importance: source, schema and configuration first; tests, fixtures, generated code and data later. The most important files are indexed until a per-repository budget is spent, so a huge repository gets its core code indexed instead of failing.' },
      { type: 'stats', items: [['10,000', 'files'], ['100 MB', 'of source'], ['15,000', 'sections']] },
      { type: 'p', text: 'These are the default limits per repository. Non-English translation catalogues, build artifacts, dependency folders such as `node_modules`, lockfiles, minified bundles and binary files are skipped. Hidden files such as `.env` are never indexed; documented templates like `.env.example` are.' },
      { type: 'callout', title: 'When your workspace is nearly full', text: 'Your plan limit becomes the budget for a new repository: its most important files are indexed up to the space left, and the rest are listed as over budget. When no space is left, indexing stops with a message. Delete a repository or move to Team to make room.' },
      { type: 'callout', title: 'Nothing is dropped silently', text: 'Every omitted file is listed with its reason in the coverage report, so you always know what the index does and does not contain.' },
      { type: 'h2', text: 'Keyword first, semantic next' },
      {
        type: 'steps',
        items: [
          ['Download and select', 'The repository archive is fetched from GitHub and files are ranked against the budget.'],
          ['Split into sections', 'Files are divided into source-aware sections that keep their path, symbols and exact line ranges.'],
          ['Keyword search is ready', 'The keyword (full-text) index is written first, so you can search and ask within moments.'],
          ['Semantic index builds', 'NVIDIA Nemotron embeddings are generated in the background, with live progress.'],
        ],
      },
      { type: 'h2', text: 'Refreshing an index' },
      { type: 'p', text: 'Re-indexing compares file hashes and only re-processes files that changed. The previous index stays queryable while the refresh runs, so questions keep working during an update.' },
    ],
  },
  {
    id: 'asking',
    label: 'Asking questions',
    icon: MessageSquareText,
    eyebrow: 'Asking questions',
    title: 'Answers that show their work.',
    summary: 'Pick a workflow and ask in plain language. Every claim cites `file:line` ranges checked against the indexed code.',
    blocks: [
      { type: 'h2', text: 'Workflows' },
      { type: 'p', text: 'A workflow focuses retrieval and shapes how the answer is organised.' },
      {
        type: 'list',
        items: [
          ['General question', 'Ask how any part of the code works. Get the answer with the lines behind it.'],
          ['New engineer onboarding', 'Map entry points, module layout and the concepts the rest of the code depends on.'],
          ['Security review', 'Trace authentication, input handling and other sensitive paths to the exact lines.'],
          ['Architecture deep dive', 'Name the components, their boundaries and how data moves between them.'],
          ['Open-source contributor', 'Find where a change belongs and which conventions it has to follow.'],
          ['Technical due diligence', 'Assess structure, risk and code quality from the source, not the README.'],
        ],
      },
      { type: 'h2', text: 'Citations and sources' },
      { type: 'p', text: 'Answers stream in real time. Claims cite code with its path and line range:' },
      { type: 'pills', items: ['src/server/auth.ts:L12-L40', 'packages/db/schema.prisma:L88-L104'] },
      { type: 'p', text: 'Every citation opens the exact lines in the built-in file viewer and links to GitHub at the indexed commit, so the code you read is the code the answer was based on. Sources are grouped by folder, which makes it easy to see which parts of the repository an answer relied on.' },
      { type: 'callout', title: 'Follow-up questions', text: 'Ask follow-ups in the same conversation; recent messages are kept in context. Conversations are saved per repository, so you can pick up where you left off.' },
    ],
  },
  {
    id: 'search',
    label: 'Search, overview & impact',
    icon: Search,
    eyebrow: 'Search, overview & impact',
    title: 'Find your way around before you ask.',
    summary: 'Code search, a repository overview and change-impact analysis, all built from the index.',
    blocks: [
      {
        type: 'list',
        items: [
          ['Instant code search', 'Search the indexed code by keyword and see the matching files and lines right away.'],
          ['Repository overview', 'See the languages, a folder map and suggested questions to start with.'],
          ['Change-impact analysis', 'Pick a file to see which files import it, before you change it.'],
        ],
      },
      { type: 'callout', title: 'Tip', text: 'Use impact analysis before an edit: the files that import the one you are changing are the call sites to check.' },
    ],
  },
  {
    id: 'editing',
    label: 'Code editing & pull requests',
    icon: GitPullRequest,
    eyebrow: 'Code editing & pull requests',
    title: 'From a described change to a reviewed pull request.',
    summary: 'The agent drafts edits as red/green diffs. You review and edit them, then open the pull request in one click.',
    blocks: [
      {
        type: 'steps',
        items: [
          ['Describe the change', 'Explain what you want in plain language, or paste a GitHub issue URL and its text becomes the brief.', ['https://github.com/owner/repo/issues/123']],
          ['The agent plans', 'It decides which files must change across the repository, including call sites and tests, up to 8 files per change.'],
          ['Exact, validated hunks', 'It reads the current files from GitHub and writes exact search/replace hunks. Every hunk is validated against the real file; nothing unvalidated is shown as a patch.'],
          ['Review the diff', 'Changes appear as a GitHub-style green and red diff, unified or split. You can edit the result before pushing.'],
          ['Open a pull request', 'Connect GitHub from the review when you are ready. With write access the PR comes from a new branch in your repository; otherwise from a fork. Or download a `.patch` instead.'],
        ],
      },
      { type: 'callout', title: 'Nothing is pushed without your review', text: 'The agent never writes to GitHub on its own. A pull request is created only when you open it from the review screen.' },
    ],
  },
  {
    id: 'security',
    label: 'Security & privacy',
    icon: ShieldCheck,
    eyebrow: 'Security & privacy',
    title: 'Your workspace stays yours.',
    summary: 'Account-scoped data, encrypted tokens and narrow permissions for every pull request.',
    blocks: [
      {
        type: 'list',
        items: [
          ['Account-scoped workspaces', 'Repositories, conversations and usage belong to your account and are visible only to you.'],
          ['Encrypted GitHub tokens', 'If you connect GitHub, the token is encrypted at rest and never sent to your browser. You can disconnect at any time from your account page.'],
          ['File-scoped PR tickets', 'Reading files for a review and opening a pull request require a short-lived ticket scoped to the files in that change.'],
          ['Untrusted repository content', 'Code, comments, documentation and issues are treated as untrusted input, and the agent is instructed never to act on instructions found inside them.'],
        ],
      },
    ],
  },
  {
    id: 'shortcuts',
    label: 'Keyboard shortcuts',
    icon: Keyboard,
    eyebrow: 'Keyboard shortcuts',
    title: 'Keep your hands on the keyboard.',
    summary: 'The shortcuts that work across the workspace.',
    blocks: [
      {
        type: 'shortcuts',
        items: [
          [[['⌘', 'K'], ['Ctrl', 'K']], 'Open the command palette: switch repository, add one, or jump to a page'],
          [[['Enter']], 'Send your question (on a touch keyboard, use ⌘ or Ctrl with Enter)'],
          [[['Shift', 'Enter']], 'Insert a new line'],
          [[['↑']], 'Recall your last question when the box is empty'],
          [[['Esc']], 'Stop an answer while it streams; otherwise close panels and dialogs'],
        ],
      },
    ],
  },
];


const SECTION_IDS = new Set(SECTIONS.map((section) => section.id));
const INTRO_CARDS = [
  { id: 'getting-started', icon: Rocket, text: 'Sign in and add your first repository.' },
  { id: 'indexing', icon: Layers, text: 'How large repositories are indexed within a budget.' },
  { id: 'asking', icon: MessageSquareText, text: 'Workflows, streaming answers and citations.' },
  { id: 'editing', icon: GitPullRequest, text: 'Turn a change into a reviewed pull request.' },
];

const slug = (sectionId, text) => `${sectionId}-${text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '')}`;
const prefersReducedMotion = () => Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
const scrollBehavior = () => (prefersReducedMotion() ? 'auto' : 'smooth');
// Distance from the top of the viewport that counts as "reading this heading":
// just below the sticky site header (and the section tabs on small screens).
const READING_LINE = 140;

/** Map a URL hash (a section id or one of its sub-heading ids) to a section. */
const resolveSectionId = (hash) => {
  if (SECTION_IDS.has(hash)) return hash;
  const owner = SECTIONS.find((section) => section.blocks.some((block) => block.type === 'h2' && slug(section.id, block.text) === hash));
  return owner ? owner.id : SECTIONS[0].id;
};

/** Text with `backtick` spans rendered as inline code. */
const InlineText = ({ text }) => String(text).split(/`([^`]+)`/).map((part, index) => (index % 2
  ? <code key={index} className="rounded-md border border-line bg-surface-2 px-1.5 py-px font-mono text-[0.86em] text-ink [overflow-wrap:anywhere]">{part}</code>
  : part));

/** Copy to the clipboard and remember which item was copied for a moment. */
const useCopied = () => {
  const [copied, setCopied] = useState(false);
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async (value) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      return false;
    }
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1800);
    return true;
  };
  return [copied, copy];
};

const CodePill = ({ children, copyable = false }) => {
  const [copied, copy] = useCopied();
  if (!copyable) {
    return (
      <code className="inline-flex max-w-full items-center rounded-lg border border-line bg-surface-2 px-2 py-1 font-mono text-[12.5px] leading-5 text-ink [overflow-wrap:anywhere]">
        {children}
      </code>
    );
  }
  return (
    <span className="inline-flex max-w-full items-stretch overflow-hidden rounded-lg border border-line bg-surface-2">
      <code className="min-w-0 px-2 py-1 font-mono text-[12.5px] leading-5 text-ink [overflow-wrap:anywhere]">{children}</code>
      <button
        type="button"
        onClick={() => void copy(children)}
        className="flex w-8 shrink-0 items-center justify-center border-l border-line text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink"
        aria-label={`Copy ${children}`}
        title="Copy"
      >
        {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
      </button>
      <span role="status" className="sr-only">{copied ? 'Copied to the clipboard' : ''}</span>
    </span>
  );
};

/** A sub-heading with a shareable link: the button copies /docs#id. */
const AnchorHeading = ({ id, text, onJump }) => {
  const [copied, copy] = useCopied();
  const share = (event) => {
    onJump(event, id);
    void copy(`${window.location.origin}/docs#${id}`);
  };
  return (
    <div className="group flex flex-wrap items-center gap-x-2 pt-4">
      <h2 id={id} tabIndex={-1} className="section-title scroll-mt-32 text-xl text-ink sm:text-2xl">{text}</h2>
      <a
        href={`#${id}`}
        onClick={share}
        className={`flex h-7 items-center gap-1.5 rounded-md px-1.5 text-xs text-ink-3 transition-[opacity,color,background-color] hover:bg-surface-2 hover:text-ink focus-visible:opacity-100 sm:group-hover:opacity-100 ${copied ? 'sm:opacity-100' : 'sm:opacity-0'}`}
        aria-label={`Copy link to “${text}”`}
        title="Copy link to this heading"
      >
        {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" /> : <Link2 className="h-3.5 w-3.5" aria-hidden="true" />}
        {copied && <span className="text-success">Link copied</span>}
      </a>
      <span role="status" className="sr-only">{copied ? 'Link copied' : ''}</span>
    </div>
  );
};

const ShortcutKeys = ({ combos }) => (
  <span className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
    {combos.map((keys, comboIndex) => (
      <span key={keys.join('+')} className="flex items-center gap-2">
        {comboIndex > 0 && <span className="text-xs text-ink-3">or</span>}
        <span className="flex items-center gap-1">
          {keys.map((key) => <Kbd key={key} className="h-6 min-w-6 px-1.5 text-[11px]">{key}</Kbd>)}
        </span>
      </span>
    ))}
  </span>
);

const Block = ({ block, sectionId, onSelect, onJump, itemHeading: ItemHeading = 'h3' }) => {
  switch (block.type) {
    case 'p':
      return <p className="text-[15.5px] leading-[1.75] text-ink-2"><InlineText text={block.text} /></p>;
    case 'h2':
      return <AnchorHeading id={slug(sectionId, block.text)} text={block.text} onJump={onJump} />;
    case 'pills':
      return (
        <div>
          {block.label && <p className="mb-2 text-[13px] font-medium text-ink-2">{block.label}</p>}
          <div className="flex flex-wrap gap-2">{block.items.map((item) => <CodePill key={item}>{item}</CodePill>)}</div>
        </div>
      );
    case 'steps':
      return (
        <ol className="relative space-y-1">
          {block.items.map(([title, text, pills], index) => (
            <li key={title} className="relative grid grid-cols-[2rem_minmax(0,1fr)] gap-4 pb-6 last:pb-0">
              {index < block.items.length - 1 && (
                <span className="absolute left-4 top-9 bottom-1 w-px -translate-x-1/2 bg-line" aria-hidden="true" />
              )}
              <span className="relative flex h-8 w-8 items-center justify-center rounded-full border border-line bg-surface font-mono text-[12px] font-semibold text-accent-strong shadow-[var(--shadow-soft)]" aria-hidden="true">
                {index + 1}
              </span>
              <div className="min-w-0 pt-1">
                <ItemHeading className="text-[15px] font-semibold text-ink">{title}</ItemHeading>
                <p className="mt-1.5 text-[15px] leading-relaxed text-ink-2"><InlineText text={text} /></p>
                {/* Step examples are things to paste, so each one can be copied. */}
                {pills && <div className="mt-3 flex flex-wrap gap-2">{pills.map((pill) => <CodePill key={pill} copyable>{pill}</CodePill>)}</div>}
              </div>
            </li>
          ))}
        </ol>
      );
    case 'list':
      return (
        <ul className="grid gap-3 sm:grid-cols-2">
          {block.items.map(([title, text]) => (
            <li key={title} className="rounded-2xl border border-line bg-surface p-4 shadow-[var(--shadow-soft)]">
              <ItemHeading className="text-[14.5px] font-semibold text-ink">{title}</ItemHeading>
              <p className="mt-1 text-sm leading-relaxed text-ink-2"><InlineText text={text} /></p>
            </li>
          ))}
        </ul>
      );
    case 'callout':
      // role="note" marks a callout without adding a landmark for each one.
      return (
        <div role="note" className="flex gap-3 rounded-2xl border border-accent/20 bg-accent-soft p-4 sm:p-5">
          <Lightbulb className="mt-0.5 h-4 w-4 shrink-0 text-accent-strong" aria-hidden="true" />
          <div className="min-w-0">
            <p className="text-sm font-semibold text-ink">{block.title}</p>
            <p className="mt-1 text-sm leading-relaxed text-ink-2"><InlineText text={block.text} /></p>
          </div>
        </div>
      );
    case 'stats':
      return (
        <dl className="grid grid-cols-3 overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-soft)]">
          {block.items.map(([value, label], index) => (
            <div key={label} className={`flex flex-col-reverse px-3 py-4 text-center sm:px-5 sm:py-5 ${index > 0 ? 'border-l border-line' : ''}`}>
              <dt className="mt-1 text-xs text-ink-2 sm:text-[13px]">{label}</dt>
              <dd className="display-title text-xl text-ink sm:text-3xl">{value}</dd>
            </div>
          ))}
        </dl>
      );
    case 'cards':
      return (
        <ul className="grid gap-3 sm:grid-cols-2">
          {INTRO_CARDS.map(({ id, icon: Icon, text }) => {
            const target = SECTIONS.find((section) => section.id === id);
            return (
              <li key={id}>
                <button
                  type="button"
                  onClick={() => onSelect(id)}
                  className="group flex h-full w-full items-start gap-3 rounded-2xl border border-line bg-surface p-4 text-left shadow-[var(--shadow-soft)] transition-[border-color,box-shadow] duration-150 hover:border-line-strong hover:shadow-[var(--shadow-lift)]"
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent-strong">
                    <Icon className="h-4 w-4" aria-hidden="true" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5 text-[14.5px] font-semibold text-ink">
                      {target.label}
                      <ArrowRight className="h-3.5 w-3.5 text-ink-3 transition-transform duration-150 group-hover:translate-x-0.5" aria-hidden="true" />
                    </span>
                    <span className="mt-1 block text-sm leading-relaxed text-ink-2">{text}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      );
    case 'shortcuts':
      return (
        <div className="overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-soft)]">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Keyboard shortcuts</caption>
            <thead className="bg-surface-2 text-xs text-ink-2">
              <tr>
                <th scope="col" className="px-4 py-2.5 font-medium">Shortcut</th>
                <th scope="col" className="px-4 py-2.5 font-medium">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {block.items.map(([combos, description]) => (
                <tr key={description}>
                  <td className="px-4 py-3.5 align-middle"><ShortcutKeys combos={combos} /></td>
                  <td className="px-4 py-3.5 align-middle text-ink-2">{description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return null;
  }
};

/** The sub-heading currently being read, for the "On this page" lists. */
const useActiveHeading = (ids) => {
  const key = ids.join('|');
  const [active, setActive] = useState(ids[0] || '');
  useEffect(() => {
    const list = key ? key.split('|') : [];
    if (list.length < 2) {
      setActive(list[0] || '');
      return undefined;
    }
    let frame = 0;
    const measure = () => {
      frame = 0;
      let current = list[0];
      list.forEach((id) => {
        const element = document.getElementById(id);
        if (element && element.getBoundingClientRect().top <= READING_LINE) current = id;
      });
      // A short last section may never reach the reading line.
      const atBottom = window.scrollY > 0 && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
      setActive(atBottom ? list[list.length - 1] : current);
    };
    const onScroll = () => {
      if (!frame) frame = window.requestAnimationFrame(measure);
    };
    measure();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [key]);
  return active;
};

const GuideNavigation = ({ activeId, onSelect }) => (
  <nav aria-label="Documentation sections">
    <p className="eyebrow mb-3 px-3">Guide</p>
    <ul className="space-y-0.5">
      {SECTIONS.map(({ id, label, icon: Icon }) => {
        const active = id === activeId;
        return (
          <li key={id}>
            <button
              type="button"
              onClick={() => onSelect(id)}
              aria-current={active ? 'page' : undefined}
              className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[13.5px] transition-colors ${active
                ? 'bg-surface font-medium text-ink shadow-[var(--shadow-soft)] ring-1 ring-line'
                : 'text-ink-2 hover:bg-surface-2 hover:text-ink'}`}
            >
              <Icon className={`h-4 w-4 shrink-0 ${active ? 'text-accent' : ''}`} aria-hidden="true" />
              <span className="min-w-0">{label}</span>
            </button>
          </li>
        );
      })}
    </ul>
  </nav>
);

const MobileTabs = ({ activeId, onSelect }) => {
  const listRef = useRef(null);
  // Fades show that the strip scrolls, and only on the side that has more.
  const [overflow, setOverflow] = useState({ start: false, end: false });

  useEffect(() => {
    const list = listRef.current;
    if (!list) return undefined;
    const measure = () => {
      const max = list.scrollWidth - list.clientWidth;
      setOverflow({ start: list.scrollLeft > 4, end: list.scrollLeft < max - 4 });
    };
    measure();
    list.addEventListener('scroll', measure, { passive: true });
    window.addEventListener('resize', measure);
    return () => {
      list.removeEventListener('scroll', measure);
      window.removeEventListener('resize', measure);
    };
  }, []);

  useEffect(() => {
    // Center the active tab by scrolling only the tab strip (never the page).
    const list = listRef.current;
    const active = list?.querySelector('[aria-current="page"]');
    if (!list || !active || !list.clientWidth) return;
    const listRect = list.getBoundingClientRect();
    const activeRect = active.getBoundingClientRect();
    const delta = (activeRect.left + activeRect.width / 2) - (listRect.left + listRect.width / 2);
    if (Math.abs(delta) > 1) list.scrollBy({ left: delta, behavior: scrollBehavior() });
  }, [activeId]);

  return (
    <nav aria-label="Documentation sections" className="sticky top-14 z-30 -mx-4 border-b border-line bg-canvas/90 backdrop-blur-md sm:-mx-8 lg:hidden">
      <div className="relative">
        <ul ref={listRef} className="scroll-thin flex gap-1 overflow-x-auto px-4 py-2.5 sm:px-8">
          {SECTIONS.map(({ id, label }) => {
            const active = id === activeId;
            return (
              <li key={id} className="shrink-0">
                <button
                  type="button"
                  onClick={() => onSelect(id)}
                  aria-current={active ? 'page' : undefined}
                  className={`h-8 whitespace-nowrap rounded-full px-3.5 text-[13px] font-medium transition-colors ${active
                    ? 'bg-ink text-canvas'
                    : 'text-ink-2 hover:bg-surface-2 hover:text-ink'}`}
                >
                  {label}
                </button>
              </li>
            );
          })}
        </ul>
        <span className={`pointer-events-none absolute inset-y-0 left-0 w-8 bg-linear-to-r from-canvas to-transparent transition-opacity ${overflow.start ? 'opacity-100' : 'opacity-0'}`} aria-hidden="true" />
        <span className={`pointer-events-none absolute inset-y-0 right-0 w-10 bg-linear-to-l from-canvas to-transparent transition-opacity ${overflow.end ? 'opacity-100' : 'opacity-0'}`} aria-hidden="true" />
      </div>
    </nav>
  );
};

/** "On this page": a rail on wide screens, a compact box above the content otherwise. */
const OnThisPage = ({ items, activeHeading, onJump, compact = false }) => (
  <nav aria-label="On this page" className={compact ? 'rounded-2xl border border-line bg-surface/80 px-4 py-3.5' : ''}>
    <p className="text-xs font-semibold text-ink">On this page</p>
    <ul className={compact ? 'mt-2 flex flex-col gap-1.5 sm:flex-row sm:flex-wrap sm:gap-x-5' : 'mt-3 space-y-2 border-l border-line'}>
      {items.map(({ id, text }) => {
        const active = id === activeHeading;
        return (
          <li key={id}>
            <a
              href={`#${id}`}
              onClick={(event) => onJump(event, id)}
              aria-current={!compact && active ? 'location' : undefined}
              className={compact
                ? 'inline-flex items-center gap-1.5 rounded text-[13px] text-ink-2 transition-colors hover:text-accent-strong'
                : `-ml-px block border-l pl-3 text-[13px] leading-snug transition-colors ${active
                  ? 'border-accent font-medium text-ink'
                  : 'border-transparent text-ink-2 hover:border-line-strong hover:text-ink'}`}
            >
              {compact && <ArrowRight className="h-3 w-3 shrink-0 text-ink-3" aria-hidden="true" />}
              {text}
            </a>
          </li>
        );
      })}
    </ul>
  </nav>
);

const DocsPage = () => {
  const user = useStore((state) => state.user);
  const location = useLocation();
  const navigate = useNavigate();
  const headingRef = useRef(null);
  const articleRef = useRef(null);
  const selectedByUser = useRef(false);
  const jumpingRef = useRef(false);

  // The active section lives in the URL hash so sections can be linked to
  // and the browser's back button moves between them.
  let hashId = '';
  try {
    hashId = decodeURIComponent(location.hash.replace(/^#/, ''));
  } catch {
    hashId = '';
  }
  const activeId = resolveSectionId(hashId);
  const lastSectionRef = useRef(activeId);
  const activeIndex = SECTIONS.findIndex((section) => section.id === activeId);
  const activeSection = SECTIONS[activeIndex];
  const SectionIcon = activeSection.icon;
  const previous = SECTIONS[activeIndex - 1];
  const next = SECTIONS[activeIndex + 1];
  const subheadings = activeSection.blocks
    .filter((block) => block.type === 'h2')
    .map((block) => ({ id: slug(activeSection.id, block.text), text: block.text }));
  const activeHeading = useActiveHeading(subheadings.map((item) => item.id));
  // Keep heading levels sequential: item titles are h3 under a sub-heading
  // and h2 when they sit directly under the section's h1.
  const itemHeadingAt = (index) => (activeSection.blocks.slice(0, index).some((block) => block.type === 'h2') ? 'h3' : 'h2');

  // Bring the section into view; move focus to its heading when the reader
  // chose it, so keyboard and screen-reader users land on what changed.
  const revealArticle = (moveFocus) => {
    const top = articleRef.current?.getBoundingClientRect().top ?? 0;
    if (top < 0 || top > window.innerHeight * 0.6) {
      window.scrollTo({ top: Math.max(0, window.scrollY + top - 120), behavior: scrollBehavior() });
    }
    if (moveFocus) headingRef.current?.focus({ preventScroll: true });
  };

  const selectSection = (id) => {
    if (id === activeId) {
      revealArticle(true);
      return;
    }
    selectedByUser.current = true;
    navigate({ hash: id === SECTIONS[0].id ? '' : id });
  };

  // One place reacts to the hash: a sub-heading link (on first load, typed
  // into the address bar, or reached with Back) scrolls to that heading,
  // which the lazily loaded page cannot rely on the browser to do; a new
  // section brings the article into view.
  useEffect(() => {
    const sectionChanged = lastSectionRef.current !== activeId;
    lastSectionRef.current = activeId;
    if (jumpingRef.current) {
      jumpingRef.current = false;
      return;
    }
    if (hashId && !SECTION_IDS.has(hashId)) {
      document.getElementById(hashId)?.scrollIntoView({ block: 'start' });
      return;
    }
    if (sectionChanged) {
      const moveFocus = selectedByUser.current;
      selectedByUser.current = false;
      revealArticle(moveFocus);
    }
    // revealArticle only reads refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hashId, activeId]);

  const jumpTo = (event, id) => {
    event.preventDefault();
    const target = document.getElementById(id);
    if (!target) return;
    if (id !== hashId) {
      jumpingRef.current = true;
      navigate({ hash: id }, { replace: true });
    }
    target.scrollIntoView({ block: 'start', behavior: scrollBehavior() });
    target.focus({ preventScroll: true });
  };

  return (
    <main className="relative">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-80" aria-hidden="true">
        <div className="bg-grid absolute inset-0 opacity-60" />
      </div>

      <div className="content-shell relative grid gap-8 pb-20 lg:grid-cols-[14rem_minmax(0,1fr)] lg:gap-12 xl:grid-cols-[14rem_minmax(0,1fr)_12rem]">
        <aside className="hidden lg:block" aria-label="Guide">
          <div className="scroll-thin sticky top-14 max-h-[calc(100vh-3.5rem)] overflow-y-auto py-10 pr-1">
            <GuideNavigation activeId={activeId} onSelect={selectSection} />
            <div className="mt-8 rounded-2xl border border-line bg-surface p-4 shadow-[var(--shadow-soft)]">
              <p className="text-[13px] font-semibold text-ink">{user ? 'Try it on your code' : 'Try it on a real repository'}</p>
              <p className="mt-1 text-xs leading-relaxed text-ink-2">
                {user ? 'Open your workspace and add a public repository.' : 'Sign in with Google and paste any public GitHub repository.'}
              </p>
              <div className="mt-3">
                {user ? (
                  <Button to="/app" variant="dark" size="sm" className="w-full" iconRight={ArrowRight}>Open workspace</Button>
                ) : (
                  <GoogleSignInButton size="sm" className="w-full" label="Sign in with Google" />
                )}
              </div>
            </div>
          </div>
        </aside>

        <div className="min-w-0">
          <MobileTabs activeId={activeId} onSelect={selectSection} />

          <article ref={articleRef} key={activeSection.id} className="mx-auto max-w-[46rem] animate-rise pt-10 sm:pt-14" aria-labelledby="docs-section-title">
            <header>
              <p className="eyebrow flex items-center gap-2">
                <SectionIcon className="h-3.5 w-3.5" aria-hidden="true" />
                {activeSection.eyebrow}
                <span className="font-sans font-medium normal-case tracking-normal text-ink-3 lg:hidden">· {activeIndex + 1} of {SECTIONS.length}</span>
              </p>
              <h1
                id="docs-section-title"
                ref={headingRef}
                tabIndex={-1}
                className="display-title mt-4 text-balance text-[2.15rem] text-ink focus:outline-none sm:text-5xl"
              >
                {activeSection.title}
              </h1>
              <p className="mt-5 text-lg leading-relaxed text-ink-2"><InlineText text={activeSection.summary} /></p>
            </header>

            {subheadings.length > 1 && (
              <div className="mt-8 xl:hidden">
                <OnThisPage items={subheadings} activeHeading={activeHeading} onJump={jumpTo} compact />
              </div>
            )}

            <div className="mt-10 space-y-6 border-t border-line pt-10">
              {activeSection.blocks.map((block, index) => (
                <Block
                  key={`${block.type}-${index}`}
                  block={block}
                  sectionId={activeSection.id}
                  onSelect={selectSection}
                  onJump={jumpTo}
                  itemHeading={itemHeadingAt(index)}
                />
              ))}
            </div>

            <nav aria-label="Previous and next sections" className="mt-14 grid gap-3 border-t border-line pt-8 sm:grid-cols-2">
              {previous ? (
                <button
                  type="button"
                  onClick={() => selectSection(previous.id)}
                  className="group flex flex-col items-start rounded-2xl border border-line bg-surface p-4 text-left transition-colors hover:border-line-strong"
                >
                  <span className="flex items-center gap-1.5 text-xs text-ink-3">
                    <ArrowLeft className="h-3.5 w-3.5 transition-transform duration-150 group-hover:-translate-x-0.5" aria-hidden="true" />
                    Previous
                  </span>
                  <span className="mt-1 text-[14.5px] font-semibold text-ink">{previous.label}</span>
                </button>
              ) : <span className="hidden sm:block" aria-hidden="true" />}
              {next && (
                <button
                  type="button"
                  onClick={() => selectSection(next.id)}
                  className="group flex flex-col items-end rounded-2xl border border-line bg-surface p-4 text-right transition-colors hover:border-line-strong"
                >
                  <span className="flex items-center gap-1.5 text-xs text-ink-3">
                    Next
                    <ArrowRight className="h-3.5 w-3.5 transition-transform duration-150 group-hover:translate-x-0.5" aria-hidden="true" />
                  </span>
                  <span className="mt-1 text-[14.5px] font-semibold text-ink">{next.label}</span>
                </button>
              )}
            </nav>
          </article>
        </div>

        <aside className="hidden xl:block" aria-label="In this section">
          <div className="sticky top-14 py-10">
            {subheadings.length > 1 ? (
              <OnThisPage items={subheadings} activeHeading={activeHeading} onJump={jumpTo} />
            ) : (
              <>
                <p className="text-xs font-semibold text-ink">In this section</p>
                <p className="mt-3 text-[13px] leading-relaxed text-ink-2"><InlineText text={activeSection.summary} /></p>
              </>
            )}
            <div className="mt-8 flex items-center gap-2 text-xs text-ink-2">
              <Compass className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Section {activeIndex + 1} of {SECTIONS.length}</span>
            </div>
          </div>
        </aside>
      </div>
    </main>
  );
};

export default DocsPage;
