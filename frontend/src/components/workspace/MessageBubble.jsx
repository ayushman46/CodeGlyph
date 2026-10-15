import {
  AlertTriangle, Check, CircleCheck, Copy, FileCode2, FolderTree, ImageOff, PencilLine, RotateCcw, Sparkles, CircleStop, WifiOff,
} from 'lucide-react';
import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  basename, citationExcerpt, compactPath, formatLines, groupByDirectory, matchCitation, normaliseCitationMarkdown,
  resolveReference, summariseReferences,
} from '../../lib/citations';
import { formatDuration } from '../../lib/format';
import { highlightLines } from '../../lib/prism';
import useStore from '../../store/useStore';
import CodeBlock from '../CodeBlock';
import Spinner from '../ui/Spinner';
import EditProposalCard from './EditProposalCard';

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
};

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

const useCopy = (getText) => {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(getText());
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be unavailable; the text stays selectable.
    }
  };
  return [copied, copy];
};

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
          {status === 'path' ? null : <CircleCheck className="h-3 w-3" aria-hidden="true" />}
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
  const suffix = label.slice(reference.path.length);
  return (
    <>
      <button
        type="button"
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
        aria-label={label}
        aria-describedby={anchor ? previewId : undefined}
        className={`mx-0.5 inline-flex max-w-[min(100%,26rem)] items-center gap-1 rounded-md border bg-surface-2 px-1.5 py-[1px] align-baseline font-mono text-[0.8em] text-ink-2 transition-colors hover:border-accent/50 hover:bg-accent-soft hover:text-accent-strong ${verified ? 'border-line' : 'border-dashed border-line-strong'}`}
      >
        <FileCode2 className="h-3 w-3 shrink-0 opacity-70" aria-hidden="true" />
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
      <div className="flex items-center gap-2.5 text-[13px] text-ink-2">
        <Spinner className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span role="status" aria-live="polite" className="min-w-0">{detail}</span>
        {elapsed > 0 && <span className="shrink-0 font-mono text-[11.5px] tabular-nums text-ink-3" aria-hidden="true">{elapsed}s</span>}
      </div>
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

const ReferenceCheck = ({ references }) => {
  const summary = summariseReferences(references);
  if (!summary) return null;
  const missing = [];
  const seen = new Set();
  references.forEach((item) => {
    if (item && item.valid !== true && !seen.has(item.text)) {
      seen.add(item.text);
      missing.push(item.text || item.file_path);
    }
  });
  if (!summary.unverified) {
    return (
      <p className="mt-3 flex items-center gap-1.5 text-[12px] text-ink-3">
        <CircleCheck className="h-3.5 w-3.5 shrink-0 text-success" aria-hidden="true" />
        {summary.verified === 1 ? 'The file reference was' : `All ${summary.verified} file references were`} checked against the indexed code
        {summary.repaired ? ` (${summary.repaired} corrected)` : ''}.
      </p>
    );
  }
  return (
    <div className="mt-3 rounded-xl border border-warning/30 bg-warning-soft px-3.5 py-2.5 text-[12.5px] leading-snug text-ink-2">
      <p className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
        <span>
          <span className="font-semibold text-ink">
            {summary.unverified} of {summary.total} reference{summary.total === 1 ? '' : 's'} could not be verified.
          </span>{' '}
          {summary.unverified === 1 ? 'It is' : 'They are'} shown with a dotted underline and no link: the file or lines are not in the indexed snapshot, so double-check before relying on {summary.unverified === 1 ? 'it' : 'them'}.
        </span>
      </p>
      {missing.length > 0 && (
        <p className="mt-1.5 flex flex-wrap gap-1 pl-5">
          {missing.slice(0, 6).map((text) => (
            <code key={text} className="max-w-full truncate rounded border border-warning/30 bg-surface px-1.5 py-px font-mono text-[11px] text-ink-2">{text}</code>
          ))}
        </p>
      )}
    </div>
  );
};

// ----------------------------------------------------------------- sources

const SourcesRow = ({ citations, onOpenSources, onOpenFile }) => {
  const groups = groupByDirectory(citations);
  const cited = citations.filter((citation) => citation.cited_in_answer);
  const featured = (cited.length ? cited : citations).slice(0, 3);
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5">
      <button
        type="button"
        onClick={() => onOpenSources(null)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-2.5 py-1 text-[12px] font-medium text-ink-2 transition-colors hover:border-line-strong hover:text-ink"
        aria-label={`Show all ${citations.length} source${citations.length === 1 ? '' : 's'} in ${groups.length} folder${groups.length === 1 ? '' : 's'}`}
      >
        <FolderTree className="h-3.5 w-3.5" aria-hidden="true" />
        {citations.length} source{citations.length === 1 ? '' : 's'} · {groups.length} folder{groups.length === 1 ? '' : 's'}
      </button>
      {featured.map((citation) => {
        const range = formatLines(citation.start_line, citation.end_line);
        return (
          <button
            key={`${citation.file_path}-${citation.start_line}`}
            type="button"
            onClick={() => onOpenFile(citation)}
            className="inline-flex min-w-0 max-w-[16rem] items-center gap-1 rounded-lg px-2 py-1 font-mono text-[11.5px] text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
            title={`${citation.file_path}${range ? ` · ${range}` : ''}`}
            aria-label={`Open ${citation.file_path}${range ? ` at ${range}` : ''}`}
          >
            <FileCode2 className="h-3 w-3 shrink-0 opacity-70" aria-hidden="true" />
            <span className="truncate">{basename(citation.file_path)}</span>
            {range && <span className="shrink-0 opacity-80">{range}</span>}
          </button>
        );
      })}
    </div>
  );
};

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

const MessageBubble = ({ message, repo, onRegenerate, isLast }) => {
  const openSources = useStore((state) => state.openSources);
  const openFile = useStore((state) => state.openFile);
  const askQuestion = useStore((state) => state.askQuestion);
  const setComposerDraft = useStore((state) => state.setComposerDraft);
  const isQuerying = useStore((state) => state.isQuerying);
  const isUser = message.role === 'user';
  const citations = message.citations || [];
  const repoName = repo?.repo_name;
  const [copied, copy] = useCopy(() => message.content || '');

  const markdown = useMemo(() => (isUser ? '' : normaliseCitationMarkdown(message.content)), [isUser, message.content]);

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
        return (
          <code {...props} className="unverified-reference" title="Unverified reference: this file or line range was not found in the indexed repository.">
            <AlertTriangle className="mr-1 inline h-3 w-3 -translate-y-px text-warning" aria-hidden="true" />
            {children}
            <span className="sr-only"> (unverified: not found in the indexed code)</span>
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
      return <code {...props}>{children}</code>;
    },
    pre: ({ children }) => <>{children}</>,
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
  }), [citations, message.answer_references, repoName, openReference]);

  if (isUser) {
    return (
      <div className="group flex flex-col items-end animate-rise">
        <div className="max-w-[min(42rem,88%)] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-ink px-4 py-2.5 text-[14.5px] leading-relaxed text-canvas">
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
  const suggestions = isLast && !message.streaming && !isError && !message.stopped && !message.interrupted ? followUps(message) : [];

  return (
    <div className="group flex gap-3 animate-rise">
      <div className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${isError ? 'bg-danger-soft text-danger' : 'bg-accent-soft text-accent'}`} aria-hidden="true">
        {isError ? <AlertTriangle className="h-3.5 w-3.5" /> : <Sparkles className="h-3.5 w-3.5" />}
      </div>
      <div className="min-w-0 flex-1">
        {message.mode === 'retrieval_fallback' && !message.streaming && (
          <Notice
            tone="warning"
            title="The model was unavailable"
            action={canRetry ? <RetryButton onClick={onRegenerate} /> : null}
          >
            This shows the most relevant code instead of a written answer.
          </Notice>
        )}

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
            <p className="mt-1 text-[13px] leading-relaxed text-ink-2">{message.error || message.content}</p>
            {canRetry && (
              <div className="mt-3 flex flex-wrap gap-2">
                <RetryButton onClick={onRegenerate} />
              </div>
            )}
          </div>
        ) : stoppedEmpty ? (
          <Notice tone="neutral" icon={CircleStop} title="Stopped before an answer was written" action={canRetry ? <RetryButton onClick={onRegenerate} label="Ask again" /> : null} />
        ) : (
          <div className="prose-answer">
            <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={components}>{markdown}</ReactMarkdown>
            {message.streaming && <span className="ml-0.5 inline-block h-4 w-[7px] translate-y-[3px] animate-blink rounded-[1px] bg-accent" aria-hidden="true" />}
          </div>
        )}

        {!message.streaming && message.interrupted && (
          <Notice tone="danger" title="The answer was cut off" action={canRetry ? <RetryButton onClick={onRegenerate} /> : null}>
            {message.error || 'The connection dropped before the answer finished.'}
          </Notice>
        )}
        {!message.streaming && message.stopped && message.partial && !message.interrupted && (
          <Notice tone="neutral" icon={CircleStop} title="Stopped — this answer is incomplete" action={canRetry ? <RetryButton onClick={onRegenerate} label="Regenerate" /> : null} />
        )}

        {hasProposal && !message.streaming && <EditProposalCard message={message} />}

        {!message.streaming && !isError && <ReferenceCheck references={message.answer_references} />}

        {citations.length > 0 && !message.streaming && (
          <SourcesRow citations={citations} onOpenSources={(index) => openSources(message.id, index)} onOpenFile={openCitation} />
        )}

        {!message.streaming && message.content && !isError && !stoppedEmpty && (
          <div className="mt-2 flex flex-wrap items-center gap-1 text-[11.5px] text-ink-3 opacity-100 transition-opacity [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100 [@media(hover:hover)]:focus-within:opacity-100">
            <button type="button" onClick={copy} className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 hover:bg-surface-2 hover:text-ink" aria-label={copied ? 'Answer copied' : 'Copy answer'}>
              {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
              <span aria-live="polite">{copied ? 'Copied' : 'Copy'}</span>
            </button>
            {canRetry && (
              <button type="button" onClick={onRegenerate} className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 hover:bg-surface-2 hover:text-ink" aria-label="Ask again">
                <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" /> Retry
              </button>
            )}
            {message.latency_ms ? <span className="px-1.5" title="Time to complete the answer">{formatDuration(message.latency_ms)}</span> : null}
            {message.model_profile === 'detailed' && <span className="px-1.5">Detailed model</span>}
          </div>
        )}

        {suggestions.length > 0 && !isQuerying && (
          <div className="mt-3 flex flex-wrap items-center gap-1.5" aria-label="Suggested follow-ups" role="group">
            {suggestions.map((prompt) => (
              <button
                key={prompt}
                type="button"
                onClick={() => void askQuestion(prompt.replace(/`/g, ''), { workflow: 'general', modelProfile: message.model_profile === 'detailed' ? 'detailed' : 'fast' })}
                className="inline-flex max-w-full items-center rounded-2xl border border-line bg-surface px-3 py-1 text-left text-[12.5px] leading-snug text-ink-2 transition-colors hover:border-accent/40 hover:bg-accent-soft hover:text-accent-strong"
              >
                {prompt.replace(/`/g, '')}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

export default memo(MessageBubble);
