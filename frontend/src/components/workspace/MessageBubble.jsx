import {
  AlertTriangle, ArrowDownToLine, Check, CircleCheck, Copy, FileCode2, FileSearch, ImageOff, Info, PencilLine, RotateCcw,
  Search, Sparkles, CircleStop, WifiOff, Zap,
} from 'lucide-react';
import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { errorMessage, searchRepository } from '../../api/client';
import { citationsToSources, shouldPaceReply } from '../../lib/ai/streamingText';
import { absenceTerms, markdownSection, searchName, splitSearchMatches, withoutInterruptionNote } from '../../lib/answerText';
import {
  answerLinkTarget, basename, citationExcerpt, compactPath, formatLines, groupByDirectory, markdownWithPermalinks,
  matchCitation, normaliseCitationMarkdown, referenceReasonLabel, resolveReference, summariseReferences,
} from '../../lib/citations';
import { highlightLines } from '../../lib/prism';
import useStore from '../../store/useStore';
import CodeBlock from '../CodeBlock';
import { LoadingState } from '../ai/LoadingState';
import { StreamingFollowUps, StreamingText, useSmoothStream } from '../ai/StreamingText';
import EditProposalCard, { ProposalToolChips, isEditRun } from './EditProposalCard';
import ShareAnswerButton from '../sharing/ShareAnswerButton';

const STAGE_LABELS = {
  retrieving: 'Searching the indexed code',
  generating: 'Writing the answer',
  planning: 'Planning the change',
  tracing: 'Tracing symbol usages',
  reading: 'Reading the current files',
  editing: 'Writing the patch',
  validating: 'Validating every hunk',
  repairing: 'Fixing hunks that did not match',
};

const ASK_STEPS = ['Search the index', 'Read the sources', 'Write the answer'];
const EDIT_STEPS = ['Find the code', 'Plan', 'Write the patch', 'Validate'];
const EDIT_STAGE_STEP = { retrieving: 0, planning: 1, tracing: 1, reading: 2, editing: 2, validating: 3, repairing: 3 };

const STATUS_COPY = {
  verified: { tone: 'text-success', label: 'Verified against the indexed code' },
  cited: { tone: 'text-success', label: 'In this answer’s sources' },
  path: { tone: 'text-ink-3', label: 'Mentioned in the answer · not among its sources' },
  outside_evidence: { tone: 'text-warning', label: 'Exists in the index but was not part of this answer’s evidence' },
};

const PROFILE_NAMES = { fast: 'Fast', detailed: 'Detailed' };

const extensionOf = (path) => {
  const name = basename(path);
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1) : name.toLowerCase();
};

/** Seconds since `startedAt`, ticking while `active`. */
const useElapsed = (startedAt, active) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return startedAt ? Math.max(0, Math.floor((now - startedAt) / 1000)) : 0;
};

// Streaming Text's caret, drawn by the Markdown at the end of its last line
// (the `data-slot="markdown"` root hides Streaming Text's own caret, which
// would otherwise sit on a line of its own under the last block).
const CARET_CLASSES = ['ms-0.5', 'inline-block', 'h-[1em]', 'w-0.5', 'translate-y-[0.15em]', 'rounded-full', 'bg-ink', 'forced-colors:bg-[CanvasText]'];
const BLOCK_CONTAINERS = new Set(['root', 'list', 'listItem', 'blockquote', 'table', 'tableRow']);
const INLINE_CONTAINERS = new Set(['paragraph', 'heading', 'tableCell']);

/** remark plugin: append the caret to the last paragraph, heading or cell. */
const remarkStreamingCaret = () => (tree) => {
  let parent = tree;
  for (;;) {
    const last = parent.children?.[parent.children.length - 1];
    if (!last) break;
    if (INLINE_CONTAINERS.has(last.type)) {
      parent = last;
      break;
    }
    if (!BLOCK_CONTAINERS.has(last.type)) break; // a code block or rule: the caret follows it
    parent = last;
  }
  if (!Array.isArray(parent.children)) return;
  parent.children.push({
    type: 'streamingCaret',
    children: [],
    data: { hName: 'span', hProperties: { ariaHidden: 'true', dataSlot: 'streaming-text-caret', className: CARET_CLASSES } },
  });
};

// Finished answers mount again each time the chat tab or the repository is
// reopened, and parsing was most of their render time. A finished answer's
// text never changes, so its syntax tree is kept and a copy handed out (the
// steps after parsing rewrite the tree in place). Bounded: long answers make
// trees of tens of kilobytes.
const PARSED_ANSWERS_MAX = 80;
const parsedAnswers = new Map();
function remarkCachedParse() {
  const parse = this.parser;
  this.parser = (document, file) => {
    let tree = parsedAnswers.get(document);
    if (tree) parsedAnswers.delete(document);
    else tree = parse(document, file);
    parsedAnswers.set(document, tree);
    if (parsedAnswers.size > PARSED_ANSWERS_MAX) parsedAnswers.delete(parsedAnswers.keys().next().value);
    return structuredClone(tree);
  };
}

const REMARK_PLUGINS = [remarkGfm, remarkCachedParse];
const REMARK_PLUGINS_STREAMING = [remarkGfm, remarkStreamingCaret];

/** A citation's GitHub permalink (pinned commit), kept for modified clicks. */
const githubHref = (citation) => (
  typeof citation?.github_url === 'string' && /^https:\/\/github\.com\//.test(citation.github_url) ? citation.github_url : undefined
);

// ------------------------------------------------------------ chip preview

/** Floating preview of the cited lines, positioned next to the chip. */
const ChipPreview = ({ id, anchor, reference, status, excerpt, loading, language }) => {
  const [position, setPosition] = useState(null);
  useLayoutEffect(() => {
    const rect = anchor.getBoundingClientRect();
    const width = Math.min(480, window.innerWidth - 24);
    const left = Math.min(Math.max(12, rect.left), window.innerWidth - width - 12);
    const spaceBelow = window.innerHeight - rect.bottom;
    const above = spaceBelow < 280 && rect.top > spaceBelow;
    setPosition(above
      ? { left, width, bottom: window.innerHeight - rect.top + 6 }
      : { left, width, top: rect.bottom + 6 });
  }, [anchor]);
  const html = useMemo(() => (excerpt ? highlightLines(excerpt.lines.join('\n'), language) : []), [excerpt, language]);
  const copy = STATUS_COPY[status] || STATUS_COPY.path;
  const range = formatLines(reference.start, reference.end);
  if (!position) return null;
  return createPortal(
    <div
      id={id}
      role="tooltip"
      style={position}
      className="pointer-events-none fixed z-[90] animate-fade-in overflow-hidden rounded-xl border border-line bg-surface shadow-[var(--shadow-lift)]"
    >
      <div className="border-b border-line px-3 py-2">
        <p className="truncate font-mono text-[12px] text-ink" title={reference.path}>
          {compactPath(reference.path, 60)}{range && <span className="text-ink-3"> · {range}</span>}
        </p>
        <p className={`mt-0.5 flex items-center gap-1 text-[11px] font-medium ${copy.tone}`}>
          {status === 'path' ? null : status === 'outside_evidence'
            ? <AlertTriangle className="h-3 w-3" aria-hidden="true" />
            : <CircleCheck className="h-3 w-3" aria-hidden="true" />}
          {copy.label}
        </p>
      </div>
      {excerpt ? (
        <div className="code-tokens bg-code py-1.5 font-mono text-[11.5px] leading-[18px]">
          {html.map((line, offset) => (
            <div key={offset} className="flex">
              <span className="w-10 shrink-0 select-none pr-2.5 text-right text-code-muted">{excerpt.firstLine + offset}</span>
              <span className="min-w-0 flex-1 overflow-hidden whitespace-pre pr-3" dangerouslySetInnerHTML={{ __html: line || ' ' }} />
            </div>
          ))}
          {excerpt.more > 0 && <p className="px-3 pt-1 text-[11px] text-code-muted">+{excerpt.more} more line{excerpt.more === 1 ? '' : 's'}</p>}
        </div>
      ) : loading ? (
        <div className="space-y-1.5 bg-code p-3" aria-hidden="true">
          {[70, 45, 60].map((width) => <div key={width} className="h-2.5 rounded bg-code-line" style={{ width: `${width}%` }} />)}
        </div>
      ) : null}
      <p className="border-t border-line bg-surface-2 px-3 py-1.5 text-[11px] text-ink-3">Click to open the file{range ? ' at these lines' : ''}</p>
    </div>,
    document.body,
  );
};

const CitationChip = ({ reference, status, label, onOpen, citation, repoName }) => {
  const [anchor, setAnchor] = useState(null);
  const timer = useRef(0);
  const previewId = useId();
  const loadFile = useStore((state) => state.loadFile);
  const fileEntry = useStore((state) => (repoName ? state.files[`${repoName}::${reference.path}`] : null));

  const excerpt = useMemo(() => {
    if (!anchor) return null;
    const fromCitation = citation ? citationExcerpt(citation, reference.start, reference.end) : null;
    if (fromCitation) return fromCitation;
    const content = fileEntry?.data?.content;
    if (typeof content !== 'string') return null;
    return citationExcerpt({ content, start_line: 1 }, reference.start || 1, reference.end || (reference.start ? reference.start : 12));
  }, [anchor, citation, fileEntry, reference.start, reference.end]);

  const show = (node) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      setAnchor(node);
      // Without an indexed excerpt, fetch the file: the preview fills in and
      // a click then opens instantly from the cache.
      if (repoName && !(citation && citationExcerpt(citation, reference.start, reference.end))) void loadFile(repoName, reference.path);
    }, 280);
  };
  const hide = () => {
    window.clearTimeout(timer.current);
    setAnchor(null);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (!anchor) return undefined;
    const onScroll = () => setAnchor(null);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
    };
  }, [anchor]);

  const verified = status === 'verified' || status === 'cited';
  const outside = status === 'outside_evidence';
  const suffix = label.slice(reference.path.length);
  let border = 'border-dashed border-line-strong';
  if (verified) border = 'border-line';
  else if (outside) border = 'border-dashed border-warning/60';
  return (
    <>
      <button
        type="button"
        data-reference-status={status}
        title={outside ? STATUS_COPY.outside_evidence.label : undefined}
        onClick={() => {
          hide();
          onOpen();
        }}
        onPointerEnter={(event) => { if (event.pointerType === 'mouse') show(event.currentTarget); }}
        onPointerLeave={hide}
        onFocus={(event) => {
          if (event.currentTarget.matches(':focus-visible')) show(event.currentTarget);
        }}
        onBlur={hide}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && anchor) {
            event.stopPropagation();
            hide();
          }
        }}
        aria-label={outside ? `${label} (exists in the index but was not part of this answer’s evidence)` : label}
        aria-describedby={anchor ? previewId : undefined}
        className={`mx-0.5 inline-flex max-w-[min(100%,26rem)] items-center gap-1 rounded-md border bg-surface-2 px-1.5 py-[1px] align-baseline font-mono text-[0.8em] text-ink-2 transition-colors hover:border-accent/50 hover:bg-accent-soft hover:text-accent-strong ${border}`}
      >
        {outside
          ? <AlertTriangle className="h-3 w-3 shrink-0 text-warning" aria-hidden="true" />
          : <FileCode2 className="h-3 w-3 shrink-0 opacity-70" aria-hidden="true" />}
        {/* Narrow screens cut the path from the left so the file name and
            line range always stay visible. */}
        <span className="min-w-0 truncate text-left [direction:rtl]"><bdi dir="ltr">{compactPath(reference.path)}</bdi></span>
        {suffix && <span className="shrink-0">{suffix}</span>}
      </button>
      {anchor && (
        <ChipPreview
          id={previewId}
          anchor={anchor}
          reference={reference}
          status={status}
          excerpt={excerpt}
          loading={Boolean(fileEntry?.loading)}
          language={citation?.language || extensionOf(reference.path)}
        />
      )}
    </>
  );
};

// ------------------------------------------------------------- streaming

const StreamProgress = ({ message, onOpenFile }) => {
  const editing = message.workflow === 'editing';
  const stage = message.stage?.stage;
  const citations = message.citations || [];
  const elapsed = useElapsed(message.started_at, true);
  const steps = editing ? EDIT_STEPS : ASK_STEPS;
  let current;
  if (editing) current = EDIT_STAGE_STEP[stage] ?? 0;
  else if (stage === 'generating') current = 2;
  else current = citations.length ? 1 : 0;
  const detail = message.stage?.detail || STAGE_LABELS[stage] || 'Working';
  const groups = citations.length ? groupByDirectory(citations).length : 0;
  const featured = citations.filter((citation) => citation.cited_in_answer !== false).slice(0, 3);
  const slow = editing ? elapsed >= 45 : elapsed >= 20;

  return (
    <div className="space-y-2.5">
      <LoadingState label={detail} startedAt={message.started_at ?? null} />
      <ol className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11.5px]" aria-label="Progress">
        {steps.map((step, index) => {
          const done = index < current;
          const active = index === current;
          return (
            <li key={step} className="flex items-center gap-1.5">
              {index > 0 && <span className={`h-px w-3 ${done || active ? 'bg-accent/50' : 'bg-line-strong'}`} aria-hidden="true" />}
              <span
                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 ${active ? 'bg-accent-soft font-medium text-accent-strong' : done ? 'text-ink-2' : 'text-ink-3'}`}
                aria-current={active ? 'step' : undefined}
              >
                {done ? <Check className="h-3 w-3" aria-hidden="true" /> : <span className={`h-1.5 w-1.5 rounded-full ${active ? 'bg-accent' : 'bg-line-strong'}`} aria-hidden="true" />}
                {step}
                {done && <span className="sr-only"> (done)</span>}
              </span>
            </li>
          );
        })}
      </ol>
      {citations.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
          <span>
            Found {citations.length} section{citations.length === 1 ? '' : 's'} in {groups} folder{groups === 1 ? '' : 's'}
            {featured.length ? ':' : ''}
          </span>
          {featured.map((citation) => (
            <button
              key={`${citation.file_path}-${citation.start_line}`}
              type="button"
              onClick={() => onOpenFile(citation)}
              className="inline-flex max-w-[14rem] items-center rounded-md border border-line bg-surface px-1.5 py-0.5 font-mono text-[11px] text-ink-2 transition-colors hover:border-accent/50 hover:text-accent-strong"
              title={`${citation.file_path}:${formatLines(citation.start_line, citation.end_line)}`}
            >
              <span className="truncate">{basename(citation.file_path)}</span>
            </button>
          ))}
        </div>
      )}
      {slow && (
        <p className="text-[12px] text-ink-3">
          {editing ? 'Multi-file changes usually take one to two minutes.' : 'Still working — broad questions can take up to a minute.'}
          {' '}You can keep reading or press Stop.
        </p>
      )}
    </div>
  );
};

// ----------------------------------------------------------------- notices

const Notice = ({ tone = 'warning', icon: Icon = AlertTriangle, title, children, action }) => {
  const tones = {
    warning: 'border-warning/30 bg-warning-soft',
    danger: 'border-danger/30 bg-danger-soft',
    neutral: 'border-line bg-surface-2',
  };
  const iconTone = { warning: 'text-warning', danger: 'text-danger', neutral: 'text-ink-3' }[tone];
  return (
    <div className={`mt-3 flex flex-wrap items-start gap-x-3 gap-y-2 rounded-xl border px-3.5 py-2.5 text-[13px] ${tones[tone]}`}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${iconTone}`} aria-hidden="true" />
      <div className="min-w-0 flex-1 basis-48 leading-snug text-ink-2">
        {title && <p className="font-semibold text-ink">{title}</p>}
        {children && <div className={title ? 'mt-0.5' : ''}>{children}</div>}
      </div>
      {action}
    </div>
  );
};

const RetryButton = ({ onClick, label = 'Try again' }) => (
  <button
    type="button"
    onClick={onClick}
    className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-line bg-surface px-3 text-[12.5px] font-semibold text-ink transition-colors hover:border-line-strong hover:bg-surface-2"
  >
    <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" /> {label}
  </button>
);

const ERROR_TITLES = {
  0: 'Could not reach the server',
  409: 'This repository is still indexing',
  422: 'This question could not be sent',
  429: 'Too many questions at once',
  503: 'The answer service is unavailable',
  504: 'The connection stalled',
};

const errorTitle = (status) => ERROR_TITLES[status] || 'This answer could not be completed';

const SMALL_BUTTON = 'inline-flex h-6 shrink-0 items-center gap-1 rounded-md border border-line bg-surface px-1.5 text-[11.5px] font-medium text-ink-2 transition-colors hover:border-line-strong hover:text-ink';
const ISSUE_CODE = 'max-w-full truncate rounded border border-warning/30 bg-surface px-1.5 py-px font-mono text-[11px] text-ink-2';

/** One row per reference or code name that needs a second look, saying what is actually wrong. */
const answerIssues = (references, identifiers) => {
  const rows = [];
  const seen = new Set();
  (Array.isArray(references) ? references : []).forEach((item) => {
    if (!item || item.valid === true || seen.has(item.text)) return;
    seen.add(item.text);
    rows.push({ kind: 'reference', key: `r:${item.text}`, item, label: referenceReasonLabel(item) });
  });
  // `CliApp` and `CliApp(Flask)` are one missing name: one row.
  (Array.isArray(identifiers) ? identifiers : []).forEach((item) => {
    const names = (Array.isArray(item?.names) && item.names.length ? item.names : [item?.text]).filter(Boolean);
    const key = `n:${names.join(',')}`;
    if (!item?.text || !names.length || seen.has(key)) return;
    seen.add(key);
    rows.push({ kind: 'name', key, item, label: 'not found in the indexed code' });
  });
  return rows;
};

/**
 * What the server checked in this answer. File references: how many exist in
 * the indexed code (never an all-clear: it checks where the answer points,
 * not what it says). Then every reference or code name that needs a second
 * look with the actual problem: lines outside this answer's evidence (opens
 * the file), a name that matches several files (lists them), a path that is
 * not in the index, or a code name not found anywhere in the indexed code
 * (one click searches for it).
 */
const AnswerCheck = ({ references, identifiers, truncated, onOpen, onSearch }) => {
  const summary = summariseReferences(references);
  const issues = answerIssues(references, identifiers);
  if (!summary && !issues.length) return null;
  const names = issues.filter((row) => row.kind === 'name').length;
  const refs = issues.length - names;
  const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
  return (
    <div data-slot="answer-check" className="mt-3 space-y-2">
      {summary && (
        <p className="flex items-start gap-1.5 text-[12px] leading-snug text-ink-3">
          <FileSearch className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>
            {truncated ? 'So far, ' : ''}
            {summary.inIndex === summary.total
              ? `${plural(summary.total, 'file/line reference')} ${summary.total === 1 ? 'exists' : 'exist'} in the indexed code`
              : `${summary.inIndex} of ${plural(summary.total, 'file/line reference')} exist in the indexed code`}
            {summary.repaired ? ` (${summary.repaired} path${summary.repaired === 1 ? '' : 's'} corrected)` : ''}.
            {' '}This checks where the answer points, not what it says about that code.
          </span>
        </p>
      )}
      {issues.length > 0 && (
        <div className="rounded-xl border border-warning/30 bg-warning-soft px-3.5 py-2.5 text-[12.5px] leading-snug text-ink-2">
          <p className="flex items-start gap-2 font-semibold text-ink">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
            {[refs ? plural(refs, 'reference') : '', names ? plural(names, 'code name') : ''].filter(Boolean).join(' and ')}
            {issues.length === 1 ? ' needs' : ' need'} a second look
          </p>
          <ul className="mt-1.5 space-y-1.5 pl-5">
            {issues.slice(0, 8).map(({ kind, key, item, label }) => (
              <li key={key} className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
                {kind === 'reference' && item.file_path && item.reason === 'range_outside_evidence' ? (
                  <button type="button" onClick={() => onOpen({ path: item.file_path, start: item.start_line, end: item.end_line })} className={`${ISSUE_CODE} hover:border-accent/50 hover:text-accent-strong`}>
                    {item.text}
                  </button>
                ) : <code className={ISSUE_CODE}>{item.text}</code>}
                <span>{label}</span>
                {kind === 'reference' && Array.isArray(item.candidates) && item.candidates.length > 0 && (
                  <span className="flex flex-wrap items-center gap-1">
                    <span className="sr-only">:</span>
                    {item.candidates.slice(0, 6).map((path) => (
                      <button key={path} type="button" onClick={() => onOpen({ path, start: null, end: null })} className={`${ISSUE_CODE} hover:border-accent/50 hover:text-accent-strong`}>
                        {path}
                      </button>
                    ))}
                    {Math.max(Number(item.candidate_count) || 0, item.candidates.length) > 6 && (
                      <span className="text-ink-3">and {Math.max(Number(item.candidate_count) || 0, item.candidates.length) - 6} more</span>
                    )}
                  </span>
                )}
                {kind === 'name' && onSearch && (
                  <button type="button" onClick={() => onSearch(item.names?.[0] || item.text)} className={SMALL_BUTTON}>
                    <Search className="h-3 w-3" aria-hidden="true" /> Search code for {item.names?.[0] || item.text}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

/**
 * Code search for one name, shown under the answer (no model call). The
 * search matches prefixes and ignores case, so only lines with the whole name
 * in the same case count as found (the rule behind "not found in the indexed
 * code"); lines with longer names that contain it are listed as partial.
 */
const CodeSearchCheck = ({ repoName, term, onOpen, onClose }) => {
  const [state, setState] = useState({ loading: true, results: [], error: '' });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, results: [], error: '' });
    searchRepository(repoName, searchName(term) || term, { signal: controller.signal })
      .then((response) => setState({ loading: false, results: response.data?.results || [], error: '' }))
      .catch((error) => {
        if (error?.code === 'ERR_CANCELED' || error?.name === 'CanceledError') return;
        setState({ loading: false, results: [], error: errorMessage(error, 'The search failed. Try again in a moment.') });
      });
    return () => controller.abort();
  }, [repoName, term]);
  const { name, exact, partial, longerNames } = useMemo(() => splitSearchMatches(state.results, term), [state.results, term]);
  const shown = exact.length ? exact : partial;
  const places = exact.length;
  const fileCount = new Set(exact.map((match) => match.file_path)).size;
  const searched = name && name !== term
    ? <><code className="font-mono">{name}</code> (the last part of <code className="font-mono">{term}</code>)</>
    : <code className="font-mono">{term}</code>;
  const code = (text) => <code key={text} className="font-mono">{text}</code>;
  return (
    <div data-slot="code-search-check" className="mt-2 rounded-xl border border-line bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-ink-2">
      <div className="flex items-start gap-2">
        <Search className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />
        <p role="status" className="min-w-0 flex-1">
          {state.loading && <>Searching the indexed code for <code className="font-mono">{term}</code>…</>}
          {!state.loading && state.error}
          {!state.loading && !state.error && (places
            ? <>Found {searched} in {places === 1 ? '1 place' : `${places} places`}{fileCount > 1 ? ` across ${fileCount} files` : ''} in the indexed code:</>
            : partial.length
              ? <>No exact match for {searched}. The search found only other names that contain it{longerNames.length
                ? <>, such as {longerNames.flatMap((text, index) => (index ? [', ', code(text)] : [code(text)]))}</>
                : ''}:</>
              : <>No match for {searched} anywhere in the indexed code.</>)}
        </p>
        <button type="button" onClick={onClose} className="shrink-0 rounded-md px-1.5 text-[11.5px] text-ink-3 hover:bg-surface hover:text-ink">Close</button>
      </div>
      {!state.loading && !state.error && shown.length > 0 && (
        <ul className="mt-1.5 space-y-1 pl-5" aria-label={places ? 'Exact matches' : 'Partial matches'}>
          {shown.slice(0, 8).map((match, index) => (
            <li key={`${match.file_path}:${match.line}:${index}`}>
              <button
                type="button"
                onClick={() => onOpen({ path: match.file_path, start: match.line, end: match.line })}
                className="flex max-w-full items-baseline gap-2 rounded-md text-left hover:text-accent-strong"
              >
                <span className="shrink-0 font-mono text-[11.5px]">{compactPath(match.file_path, 40)}{match.line ? `:${match.line}` : ''}</span>
                {match.text && <span className="min-w-0 truncate font-mono text-[11px] text-ink-3">{match.text.trim()}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

/** Plain text of a Markdown syntax node (a heading's title). */
const nodeText = (node) => (node?.type === 'text' ? node.value : (node?.children || []).map(nodeText).join(''));

/**
 * An answer heading with a "Copy section" control: it copies this heading's
 * section as Markdown, with file references as commit-pinned GitHub links.
 */
const SectionHeading = ({ level, node, children, getSection, ...props }) => {
  const Tag = `h${level}`;
  const titleId = useId();
  const [copyState, setCopyState] = useState('idle');
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const line = node?.position?.start?.line;
  if (!getSection || !line) return <Tag {...props}>{children}</Tag>;
  const title = nodeText(node).trim();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(getSection(line));
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopyState('idle'), 1600);
  };
  return (
    <Tag {...props} aria-labelledby={titleId} className="group/section">
      <span id={titleId}>{children}</span>
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={`Copy section: ${title}`}
        className="ms-2 inline-flex translate-y-[-1px] items-center gap-1 rounded-md px-1 py-0.5 align-middle text-[11px] font-medium text-ink-3 transition-opacity hover:bg-surface-2 hover:text-ink [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover/section:opacity-100 [@media(hover:hover)]:focus-visible:opacity-100"
      >
        {copyState === 'copied' ? <Check className="h-3 w-3" aria-hidden="true" /> : <Copy className="h-3 w-3" aria-hidden="true" />}
        <span aria-live="polite">{copyState === 'copied' ? 'Copied' : copyState === 'failed' ? 'Copy failed' : 'Copy section'}</span>
      </button>
    </Tag>
  );
};

const SmallActionButton = ({ onClick, icon: Icon, children, primary = false, disabled = false }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border px-3 text-[12.5px] font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${primary ? 'border-ink bg-ink text-canvas hover:opacity-90' : 'border-line bg-surface text-ink hover:border-line-strong hover:bg-surface-2'}`}
  >
    {Icon && <Icon className="h-3.5 w-3.5" aria-hidden="true" />} {children}
  </button>
);

// --------------------------------------------------------------- follow-ups

const IDENTIFIER = /^[A-Za-z_$][\w$]{3,40}$/;
const TEST_FILE = /(^|[./_-])(test|spec|e2e)s?([./_-]|$)/i;

/** Two concrete follow-ups built from what the answer actually cited. */
const followUps = (message) => {
  if (message.workflow === 'editing' || !['rag', 'partial', undefined].includes(message.mode)) return [];
  const cited = (message.citations || []).filter((citation) => citation.cited_in_answer);
  const top = cited[0];
  if (!top) return [];
  const prompts = [];
  const symbol = (top.symbols || []).find((name) => IDENTIFIER.test(name));
  if (symbol) prompts.push(`Where is \`${symbol}\` used, and what calls it?`);
  if (!TEST_FILE.test(top.file_path)) prompts.push(`How is \`${basename(top.file_path)}\` tested?`);
  return prompts.slice(0, 2);
};

// ---------------------------------------------------------------- message

/**
 * @param {object} props
 * @param {object} props.message
 * @param {object} [props.repo]
 * @param {(() => void) | null} [props.onRegenerate] Asks the last question again (last reply only).
 * @param {boolean} [props.isLast]
 * @param {(question: string, options: object) => void} [props.onAsk] Asks a new question the way
 *   the composer does (back to the end of the conversation, following the answer).
 * @param {(revealing: boolean) => void} [props.onRevealChange] Told whether this reply's paced
 *   text is still being revealed (after the stream ended too), so the conversation stays busy.
 */
const MessageBubble = ({ message, repo, onRegenerate, isLast, onAsk, onRevealChange }) => {
  const openFile = useStore((state) => state.openFile);
  const askQuestion = useStore((state) => state.askQuestion);
  const continueAnswer = useStore((state) => state.continueAnswer);
  const ask = onAsk || askQuestion;
  const setComposerDraft = useStore((state) => state.setComposerDraft);
  const isQuerying = useStore((state) => state.isQuerying);
  // The code name searched from this answer (a "Search code" check), if any.
  const [searchTerm, setSearchTerm] = useState(null);
  const isUser = message.role === 'user';
  // Only a question asked now (and its reply) rises in. History renders in
  // place: a page of 30 older messages animating at once is a visible stall.
  const [entering] = useState(() => Boolean(message.live || message.streaming));
  const editRun = isEditRun(message);
  // The conversation's length matters only while this reply streams in.
  const messageCount = useStore((state) => (!isUser && message.streaming ? state.messages.length : 0));
  const citations = message.citations || [];
  const repoName = repo?.repo_name;
  // The model failed partway through: a notice under the reply names the
  // failure, so the server's own note at the end of the text is not shown.
  const stoppedEarly = !isUser && message.mode === 'partial' && Boolean(message.answer_failure?.message);
  const replyText = isUser ? '' : (stoppedEarly ? withoutInterruptionNote(message.content) : (message.content || ''));

  // Smooth pacing only for a reply that streams in while this bubble is
  // mounted (never for history), and only while shouldPaceReply allows it:
  // a short, code-free reply in a short conversation. Once it says no (the
  // text grew long or a code block arrived) the reply renders as it arrives
  // for the rest of its life; inputs only grow, so it never switches back.
  const pacingOff = useRef(isUser || !message.streaming);
  if (!pacingOff.current && (
    editRun || message.mode === 'error'
    || !shouldPaceReply({ text: replyText, messageCount: message.streaming ? messageCount : 0 })
  )) pacingOff.current = true;
  const { text: shownText, isSettled } = useSmoothStream(replyText, { enabled: !pacingOff.current });
  const revealing = !isUser && !isSettled;
  useEffect(() => {
    if (!onRevealChange) return undefined;
    onRevealChange(revealing);
    return () => onRevealChange(false);
  }, [onRevealChange, revealing]);

  const markdown = useMemo(() => (isUser ? '' : normaliseCitationMarkdown(shownText)), [isUser, shownText]);
  // Finished: the stream ended and the paced reveal (if any) caught up.
  const settled = !message.streaming && isSettled;
  const canCopySections = settled && !isUser && message.mode !== 'error';

  // Copying links every reference to its lines on GitHub at the answer's commit.
  const linkTarget = useMemo(
    () => ({ ...(answerLinkTarget(message, repo) || {}), citations, answerReferences: message.answer_references }),
    [message, repo, citations],
  );
  const replyCopyText = useMemo(
    () => (settled ? markdownWithPermalinks(message.content || '', linkTarget) : message.content || ''),
    [settled, message.content, linkTarget],
  );
  // Read when a section is copied, so headings need not re-render per token.
  const sectionSource = useRef({ markdown, linkTarget });
  sectionSource.current = { markdown, linkTarget };
  const getSection = useCallback(
    (line) => markdownWithPermalinks(markdownSection(sectionSource.current.markdown, line), sectionSource.current.linkTarget),
    [],
  );
  const missingNames = useMemo(() => new Map(
    (Array.isArray(message.unverified_identifiers) ? message.unverified_identifiers : [])
      .filter((item) => item?.text)
      .map((item) => [String(item.text).trim(), item.names || []]),
  ), [message.unverified_identifiers]);

  const openReference = useCallback((reference) => {
    const index = matchCitation(citations, reference);
    const citation = index >= 0 ? citations[index] : null;
    openFile({
      path: citation?.file_path || reference.path,
      start: reference.start || citation?.start_line || null,
      end: reference.end || citation?.end_line || null,
    }, message.id);
  }, [citations, openFile, message.id]);

  const openCitation = useCallback((citation) => openFile({
    path: citation.file_path, start: citation.start_line || null, end: citation.end_line || null,
  }, message.id), [openFile, message.id]);

  const components = useMemo(() => ({
    code: ({ className, children, node, ...props }) => {
      const text = String(children ?? '');
      const language = /language-([\w+-]+)/.exec(className || '')?.[1];
      const isBlock = Boolean(language) || text.includes('\n');
      if (isBlock) return <CodeBlock code={text.replace(/\n$/, '')} language={language} />;
      const resolved = resolveReference(text, { citations, answerReferences: message.answer_references });
      if (resolved?.status === 'unverified') {
        const detail = resolved.candidates?.length ? `${resolved.label}: ${resolved.candidates.join(', ')}` : resolved.label;
        return (
          <code {...props} className="unverified-reference" data-reference-status="unverified" title={`Unverified reference: ${detail}.`}>
            <AlertTriangle className="mr-1 inline h-3 w-3 -translate-y-px text-warning" aria-hidden="true" />
            {children}
            <span className="sr-only"> (unverified: {detail})</span>
          </code>
        );
      }
      if (resolved) {
        const { reference } = resolved;
        const index = matchCitation(citations, reference);
        return (
          <CitationChip
            reference={reference}
            status={resolved.status}
            citation={index >= 0 ? citations[index] : null}
            repoName={repoName}
            label={reference.start ? `${reference.path}:${reference.start}${reference.end && reference.end !== reference.start ? `–${reference.end}` : ''}` : reference.path}
            onOpen={() => openReference(reference)}
          />
        );
      }
      const missing = missingNames.get(text.trim());
      if (missing) {
        // A code name that occurs nowhere in the indexed code.
        return (
          <code {...props} className="unverified-reference decoration-warning!" data-identifier-status="not-found" title={`Not found in the indexed code: ${missing.join(', ')}`}>
            <AlertTriangle className="mr-1 inline h-3 w-3 -translate-y-px text-warning" aria-hidden="true" />
            {children}
            <span className="sr-only"> (not found in the indexed code)</span>
          </code>
        );
      }
      return <code {...props}>{children}</code>;
    },
    pre: ({ children }) => <>{children}</>,
    h1: (props) => <SectionHeading level={1} getSection={canCopySections ? getSection : null} {...props} />,
    h2: (props) => <SectionHeading level={2} getSection={canCopySections ? getSection : null} {...props} />,
    h3: (props) => <SectionHeading level={3} getSection={canCopySections ? getSection : null} {...props} />,
    h4: (props) => <SectionHeading level={4} getSection={canCopySections ? getSection : null} {...props} />,
    a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
    // Remote images are never fetched automatically: indexed repository text
    // is untrusted model input and an image URL could carry the conversation
    // to another host. Show them as ordinary links instead.
    img: ({ src, alt }) => {
      const safe = typeof src === 'string' && /^https?:\/\//i.test(src) ? src : null;
      const label = alt || 'image';
      return safe ? (
        <a href={safe} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1" title={safe}>
          <ImageOff className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>{label}</span>
        </a>
      ) : <span>[{label}]</span>;
    },
    table: ({ children }) => (
      <div className="answer-table scroll-thin" role="region" aria-label="Table" tabIndex={0}>
        <table>{children}</table>
      </div>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [citations, message.answer_references, repoName, openReference, missingNames, canCopySections, getSection]);

  // The Sources disclosure: cited sections first, each opening the file
  // viewer at its lines (a modified or middle click follows the GitHub
  // permalink instead).
  const sources = useMemo(() => (isUser ? [] : citationsToSources(citations, {
    onSelect: (citation) => openCitation(citation),
    hrefFor: githubHref,
  })), [isUser, citations, openCitation]);

  const errorLabel = message.interrupted
    ? `The answer was cut off. ${message.error || 'The connection dropped before the answer finished.'}`
    : 'Stopped — this answer is incomplete.';
  const replyLabels = useMemo(() => ({ error: errorLabel }), [errorLabel]);

  const followUpPrompts = useMemo(
    () => (isUser || !isLast ? [] : followUps(message).map((prompt) => prompt.replace(/`/g, ''))),
    [isUser, isLast, message],
  );

  if (isUser) {
    return (
      <div data-slot="message" data-from="user" className={`group flex flex-col items-end ${entering ? 'animate-rise' : ''}`}>
        <div data-slot="message-content" className="max-w-[min(42rem,88%)] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-ink px-4 py-2.5 text-[14.5px] leading-relaxed text-canvas">
          {message.content}
        </div>
        <div className="mt-1 flex opacity-100 transition-opacity [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100 [@media(hover:hover)]:focus-within:opacity-100">
          <button
            type="button"
            onClick={() => setComposerDraft(message.content)}
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11.5px] text-ink-3 hover:bg-surface-2 hover:text-ink"
            aria-label="Edit this question in the composer"
          >
            <PencilLine className="h-3.5 w-3.5" aria-hidden="true" /> Edit
          </button>
        </div>
      </div>
    );
  }

  const isError = message.mode === 'error';
  const showProgress = message.streaming && !message.content;
  const hasProposal = Boolean(message.edit_suggestion || message.proposal_id);
  const stoppedEmpty = message.stopped && !message.partial;
  const canRetry = Boolean(isLast && onRegenerate);
  let replyStatus = 'complete';
  if (!settled) replyStatus = 'streaming';
  else if (message.interrupted || (message.stopped && message.partial)) replyStatus = 'error';
  const showFollowUps = settled && !isError && !message.stopped && !message.interrupted && !isQuerying;
  // An Edit & PR run keeps its artifact card while it is written and after:
  // complete with a proposal, error when it ended without one. A request
  // that failed outright or was stopped empty shows its own notice instead.
  const showArtifact = editRun && (message.streaming || hasProposal || (!isError && !stoppedEmpty));

  // Answer trust: a reply cut off at the output limit, a model that failed
  // before or partway through the answer, and the other model answering instead.
  const truncated = message.mode === 'truncated';
  const failure = message.answer_failure;
  const failedProfile = failure?.model_profile || message.model_profile;
  const question = () => {
    const messages = useStore.getState().messages;
    const index = messages.findIndex((item) => item.id === message.id);
    return messages.slice(0, index).reverse().find((item) => item.role === 'user')?.content || '';
  };
  const retryWithFast = isLast && failedProfile === 'detailed' && !isQuerying ? () => {
    const text = question();
    if (text) void ask(text, { workflow: message.workflow && message.workflow !== 'editing' ? message.workflow : 'general', modelProfile: 'fast' });
  } : null;
  const modelAnswers = !editRun && ['rag', 'partial', 'truncated', undefined].includes(message.mode);
  // Names the answer says it did not retrieve (flagged names have their own search).
  const flaggedNames = new Set([...missingNames.keys(), ...[...missingNames.values()].flat()]);
  const searchTerms = settled && modelAnswers && repoName
    ? absenceTerms(message.content || '', 6).filter((term) => !flaggedNames.has(term)).slice(0, 3)
    : [];
  const showCheck = settled && !isError;

  return (
    <div data-slot="message" data-from="assistant" className={`flex gap-3 ${entering ? 'animate-rise' : ''}`}>
      <div className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${isError ? 'bg-danger-soft text-danger' : 'bg-accent-soft text-accent'}`} aria-hidden="true">
        {isError ? <AlertTriangle className="h-3.5 w-3.5" /> : <Sparkles className="h-3.5 w-3.5" />}
      </div>
      <div className="min-w-0 flex-1">
        {message.mode === 'retrieval_fallback' && !message.streaming && (
          <Notice
            tone="warning"
            title={failure?.message || 'The model was unavailable'}
            action={(retryWithFast || canRetry) ? (
              <div className="flex flex-wrap gap-2">
                {retryWithFast && <SmallActionButton onClick={retryWithFast} icon={Zap} primary>Retry with Fast</SmallActionButton>}
                {canRetry && <RetryButton onClick={onRegenerate} />}
              </div>
            ) : null}
          >
            No answer was written. This shows the most relevant code instead.
          </Notice>
        )}

        {message.answer_notice?.message && settled && message.mode !== 'retrieval_fallback' && (
          <Notice tone="neutral" icon={Info} title={`Written by the ${PROFILE_NAMES[message.answer_notice.model_profile] || 'other'} model`}>
            {message.answer_notice.message}
          </Notice>
        )}

        {editRun && settled && <ProposalToolChips message={message} className="mb-3" />}

        {showProgress ? (
          <StreamProgress message={message} onOpenFile={openCitation} />
        ) : isError ? (
          <div role="alert" className="rounded-xl border border-danger/30 bg-danger-soft px-4 py-3">
            <p className="flex items-center gap-2 text-[13.5px] font-semibold text-danger">
              {message.error_status === 0 || message.error_status === 504
                ? <WifiOff className="h-4 w-4 shrink-0" aria-hidden="true" />
                : null}
              {errorTitle(message.error_status)}
            </p>
            <p data-slot="message-content" className="mt-1 text-[13px] leading-relaxed text-ink-2">{message.error || message.content}</p>
            {canRetry && (
              <div className="mt-3 flex flex-wrap gap-2">
                <RetryButton onClick={onRegenerate} />
              </div>
            )}
          </div>
        ) : stoppedEmpty ? (
          <Notice tone="neutral" icon={CircleStop} title="Stopped before an answer was written" action={canRetry ? <RetryButton onClick={onRegenerate} label="Ask again" /> : null} />
        ) : (
          // The transcript is already a log, so the reply is not announced
          // as it grows (announce={false}).
          // The model-unavailable and stopped-partway notices offer their own Try again.
          <StreamingText
            status={replyStatus}
            copyText={replyCopyText}
            onRetry={canRetry && message.mode !== 'retrieval_fallback' && !stoppedEarly ? onRegenerate : undefined}
            sources={sources}
            sourcesIcon={FileCode2}
            announce={false}
            labels={replyLabels}
          >
            {/* message-content: only the answer text, for the message rail's
                previews. The markdown root sits between .prose-answer and the
                first block, so it takes over the prose's "first child has no
                top margin" rule. */}
            <div data-slot="message-content">
              <div data-slot="markdown" className="[&>:first-child]:mt-0!">
                <ReactMarkdown
                  remarkPlugins={replyStatus === 'streaming' ? REMARK_PLUGINS_STREAMING : REMARK_PLUGINS}
                  skipHtml
                  components={components}
                >
                  {markdown}
                </ReactMarkdown>
              </div>
            </div>
          </StreamingText>
        )}

        {stoppedEarly && settled && (
          <Notice
            tone="warning"
            title={failure.message}
            action={(retryWithFast || canRetry) ? (
              <div className="flex flex-wrap gap-2">
                {retryWithFast && <SmallActionButton onClick={retryWithFast} icon={Zap} primary>Retry with Fast</SmallActionButton>}
                {canRetry && <RetryButton onClick={onRegenerate} />}
              </div>
            ) : null}
          >
            The answer stops there, so the rest is missing.
          </Notice>
        )}

        {truncated && settled && (
          <Notice
            tone="warning"
            title="This answer reached the length limit"
            action={(
              <SmallActionButton
                onClick={() => void continueAnswer(message.id, { modelProfile: message.model_profile === 'detailed' ? 'detailed' : 'fast' })}
                icon={ArrowDownToLine}
                primary
                disabled={isQuerying || !message.message_id}
              >
                Continue
              </SmallActionButton>
            )}
          >
            It stops before the end, so the last part is missing. Continue writes the rest into this message.
            {message.continue_error && <span role="alert" className="mt-1 block text-danger">{message.continue_error}</span>}
          </Notice>
        )}

        {showCheck && (
          <AnswerCheck
            references={message.answer_references}
            identifiers={message.unverified_identifiers}
            truncated={truncated || stoppedEarly}
            onOpen={(file) => openFile(file, message.id)}
            onSearch={repoName ? setSearchTerm : null}
          />
        )}

        {showCheck && searchTerms.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
            <span>Not in the retrieved evidence. Check the whole index:</span>
            {searchTerms.map((term) => (
              <button key={term} type="button" onClick={() => setSearchTerm(term)} className={SMALL_BUTTON} aria-pressed={searchTerm === term}>
                <Search className="h-3 w-3" aria-hidden="true" /> Search code for <code className="font-mono">{term}</code>
              </button>
            ))}
          </div>
        )}

        {showCheck && searchTerm && repoName && (
          <CodeSearchCheck repoName={repoName} term={searchTerm} onOpen={(file) => openFile(file, message.id)} onClose={() => setSearchTerm(null)} />
        )}
        {settled && !isError && !editRun && <ShareAnswerButton message={message} repo={repo} />}

        {showArtifact && <EditProposalCard message={message} />}

        <StreamingFollowUps
          prompts={showFollowUps ? followUpPrompts : []}
          onSelect={(prompt) => {
            void ask(prompt, { workflow: 'general', modelProfile: message.model_profile === 'detailed' ? 'detailed' : 'fast' });
            // The follow-up list unmounts as the question starts; keep keyboard focus in the composer.
            window.requestAnimationFrame(() => document.querySelector('[data-slot="prompt-bar-input"]')?.focus());
          }}
          className="mt-4"
        />
      </div>
    </div>
  );
};

export default memo(MessageBubble);
