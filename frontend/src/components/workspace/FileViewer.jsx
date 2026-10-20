import { AlertCircle, AlertTriangle, Check, ChevronDown, ChevronUp, Copy, ExternalLink, Link2, RotateCcw, X } from 'lucide-react';
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { isBeyondEnd } from '../../lib/lines';
import { highlightLines } from '../../lib/prism';
import { basename, dirname, githubBlobUrl } from '../../lib/citations';
import { shortSha } from '../../lib/format';
import useStore from '../../store/useStore';
import { ArtifactPanel } from '../ai/ArtifactPanel';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import { mergeRefs } from '../ui/Popover';

// Rows never wrap (whitespace-pre) and use a fixed 20px line box, so large
// files can be windowed: only the rows near the viewport are in the DOM.
const ROW_HEIGHT = 20;
const VIRTUALIZE_ABOVE = 1200;
const OVERSCAN = 60;

const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const anchorFor = (start, end) => (start ? `#L${start}${end && end !== start ? `-L${end}` : ''}` : '');
const rangeText = (start, end) => (end && end !== start ? `${start}–${end}` : `${start}`);
const inside = (number, range) => Boolean(range?.start) && number >= range.start && number <= (range.end || range.start);

// The Artifact Panel body is the code's scroll container: dark like the
// code, with the code scrollbar. No scroll anchoring: windowing resizes the
// spacer rows above the visible lines, and the browser would otherwise
// shift the scroll position by the same amount.
const CODE_BODY = '[&>[data-slot=artifact-panel-body]]:bg-code [&>[data-slot=artifact-panel-body]]:[scrollbar-width:thin] [&>[data-slot=artifact-panel-body]]:[scrollbar-color:#3a342e_transparent] [&>[data-slot=artifact-panel-body]]:[overflow-anchor:none]';

const CodeRow = ({ number, html, cited, other, selected, onSelectLine }) => {
  let rowTone = '';
  if (selected) rowTone = 'bg-[rgba(96,165,250,0.16)]';
  else if (cited) rowTone = 'bg-[var(--color-highlight)]';
  else if (other) rowTone = 'bg-[rgba(229,86,27,0.07)]';
  let gutterTone = 'text-code-muted';
  if (selected) gutterTone = 'text-code-ink shadow-[inset_3px_0_0_#60a5fa]';
  else if (cited) gutterTone = 'text-code-ink shadow-[inset_3px_0_0_var(--color-accent)]';
  else if (other) gutterTone = 'text-code-muted shadow-[inset_3px_0_0_rgba(229,86,27,0.45)]';
  return (
    <tr data-line={number} className={rowTone} style={{ height: ROW_HEIGHT }}>
      <td
        onClick={(event) => onSelectLine(number, event.shiftKey)}
        title="Select this line (Shift-click to select a range)"
        className={`sticky left-0 w-12 cursor-pointer select-none border-r border-code-line bg-code px-3 text-right align-top transition-colors hover:text-code-ink ${gutterTone}`}
      >
        {number}
      </td>
      <td className="whitespace-pre px-4 align-top" dangerouslySetInnerHTML={{ __html: html || ' ' }} />
    </tr>
  );
};

const IconButton = ({ label, onClick, children }) => (
  <button
    type="button"
    onClick={onClick}
    className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
    aria-label={label}
    title={label}
  >
    {children}
  </button>
);

/**
 * Full file at the indexed commit with the cited range highlighted, framed
 * as an Artifact Panel: the file name titles it, folder and lines describe
 * it, Copy GitHub link / Open on GitHub (at the pinned commit) sit in the
 * header. Other ranges the same answer cites in this file get a lighter tint
 * and can be stepped through; clicking line numbers selects lines for a
 * GitHub link. The panel body is the scroll container (keyboard-scrollable);
 * the file toolbar stays pinned at its top.
 */
const FileViewer = ({
  repoName, path, startLine, endLine, githubUrl, commitSha, ranges = [],
  onClose, navigation = null, autoFocus = false, panelRef, panelProps,
}) => {
  const loadFile = useStore((state) => state.loadFile);
  const entry = useStore((state) => state.files[`${repoName}::${path}`]);
  const sectionRef = useRef(null);
  const setSectionRef = useMemo(() => mergeRefs(sectionRef, panelRef), [panelRef]);
  const scrollRef = useRef(null);
  const tableRef = useRef(null);
  const toolbarRef = useRef(null);
  const [copied, setCopied] = useState('');
  const [selection, setSelection] = useState(null);
  const [lineInput, setLineInput] = useState('');
  const [lineError, setLineError] = useState('');
  const lineInputId = useId();

  // The Artifact Panel's body scrolls the code.
  useLayoutEffect(() => {
    scrollRef.current = sectionRef.current?.querySelector('[data-slot="artifact-panel-body"]') ?? null;
  }, []);

  // Every cited range of this file, with the opened one active.
  const allRanges = useMemo(() => {
    const list = [...ranges];
    if (startLine && !list.some((range) => range.start === startLine && (range.end || range.start) === (endLine || startLine))) {
      list.push({ start: startLine, end: endLine || startLine });
    }
    return list.filter((range) => range.start).sort((a, b) => a.start - b.start);
  }, [ranges, startLine, endLine]);
  const [activeIndex, setActiveIndex] = useState(() => Math.max(0, allRanges.findIndex((range) => range.start === startLine)));
  const active = allRanges[activeIndex] || (startLine ? { start: startLine, end: endLine || startLine } : null);

  useEffect(() => {
    if (repoName && path) void loadFile(repoName, path);
  }, [repoName, path, loadFile]);

  const data = entry?.data;
  const lines = useMemo(() => (data ? highlightLines(data.content || '', data.language) : []), [data]);

  const virtual = lines.length > VIRTUALIZE_ABOVE;
  const [viewport, setViewport] = useState({ top: 0, height: 800 });
  const frameRef = useRef(0);
  // Where the table starts inside the scroll container (below the pinned
  // toolbar and any notes), in scroll coordinates.
  const tableTop = useCallback(() => {
    const node = scrollRef.current;
    const table = tableRef.current;
    if (!node || !table) return 0;
    return table.getBoundingClientRect().top - node.getBoundingClientRect().top + node.scrollTop;
  }, []);
  const measure = useCallback(() => {
    frameRef.current = 0;
    const node = scrollRef.current;
    if (node) setViewport({ top: Math.max(0, node.scrollTop - tableTop()), height: node.clientHeight || 800 });
  }, [tableTop]);
  const onScroll = useCallback(() => {
    if (virtual && !frameRef.current) frameRef.current = window.requestAnimationFrame(measure);
  }, [virtual, measure]);
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return undefined;
    node.addEventListener('scroll', onScroll, { passive: true });
    return () => node.removeEventListener('scroll', onScroll);
  }, [onScroll]);
  useEffect(() => () => { if (frameRef.current) window.cancelAnimationFrame(frameRef.current); }, []);
  // The longest line fixes the table width, so windowing never makes the
  // horizontal scroll range jump.
  const widestLine = useMemo(() => (virtual ? String(data?.content || '').split('\n').reduce((max, line) => Math.max(max, line.length), 0) : 0), [virtual, data]);

  const totalLines = Number(data?.total_lines) || Number(data?.line_count) || lines.length;
  const truncated = Boolean(data?.truncated);
  const activeStart = active?.start || null;
  // Past the real end of the file (not just past a truncated view).
  const beyondEnd = Boolean(data) && isBeyondEnd(activeStart, truncated ? totalLines : lines.length);
  const beyondShown = Boolean(data) && truncated && !beyondEnd && isBeyondEnd(activeStart, lines.length);

  useEffect(() => {
    if (virtual) measure();
  }, [lines, virtual, measure]);

  // Puts the line a quarter of the way down the code below the pinned toolbar.
  const scrollToLine = useCallback((line, { smooth = true } = {}) => {
    const node = scrollRef.current;
    if (!node || !line) return;
    const pinned = toolbarRef.current?.offsetHeight || 0;
    const row = virtual ? null : node.querySelector(`[data-line="${line}"]`);
    const rowTop = row
      ? row.getBoundingClientRect().top - node.getBoundingClientRect().top + node.scrollTop
      : tableTop() + (line - 1) * ROW_HEIGHT;
    const top = Math.max(0, rowTop - pinned - (node.clientHeight - pinned) / 4);
    if (virtual) {
      node.scrollTop = top;
      setViewport({ top: Math.max(0, top - tableTop()), height: node.clientHeight || 800 });
      return;
    }
    node.scrollTo({ top, behavior: smooth && !reducedMotion() ? 'smooth' : 'auto' });
  }, [virtual, tableTop]);

  useEffect(() => {
    if (!lines.length || !activeStart || beyondEnd || beyondShown) return;
    scrollToLine(activeStart);
  }, [lines, activeStart, beyondEnd, beyondShown, scrollToLine]);

  const first = virtual ? Math.max(0, Math.floor(viewport.top / ROW_HEIGHT) - OVERSCAN) : 0;
  const last = virtual ? Math.min(lines.length, Math.ceil((viewport.top + viewport.height) / ROW_HEIGHT) + OVERSCAN) : lines.length;

  // Links and copies use the user's selected lines, else the cited range.
  const target = selection || active;
  const linkStart = target?.start || null;
  const linkEnd = target?.end || null;
  const link = data?.github_url
    ? `${data.github_url}${anchorFor(linkStart, linkEnd)}`
    : githubBlobUrl(githubUrl, commitSha, path, linkStart, linkEnd);

  const flash = (kind) => {
    setCopied(kind);
    window.setTimeout(() => setCopied(''), 1600);
  };
  const copyText = async (text, kind) => {
    try {
      await navigator.clipboard.writeText(text);
      flash(kind);
    } catch {
      // Clipboard can be unavailable.
    }
  };

  const selectLine = (number, extend) => {
    setLineError('');
    setSelection((current) => {
      if (extend && current) {
        const anchor = current.anchor ?? current.start;
        return { start: Math.min(anchor, number), end: Math.max(anchor, number), anchor };
      }
      if (current && current.start === number && current.end === number) return null;
      return { start: number, end: number, anchor: number };
    });
  };

  const goToLine = (event) => {
    event.preventDefault();
    const value = Number.parseInt(lineInput, 10);
    if (!Number.isFinite(value) || value < 1) {
      setLineError('Enter a line number.');
      return;
    }
    if (value > lines.length) {
      setLineError(lines.length < totalLines ? `Only the first ${lines.length.toLocaleString()} lines are shown here.` : `This file has ${lines.length.toLocaleString()} lines.`);
      return;
    }
    setLineError('');
    setSelection({ start: value, end: value, anchor: value });
    scrollToLine(value);
  };

  const step = (delta) => {
    if (allRanges.length < 2) return;
    const next = (activeIndex + delta + allRanges.length) % allRanges.length;
    setActiveIndex(next);
    setSelection(null);
  };

  const otherRanges = allRanges.filter((_, index) => index !== activeIndex);
  const folder = dirname(path);
  const description = (
    <span title={path}>
      {folder || 'Repository root'}
      {active ? ` · lines ${rangeText(active.start, active.end)}` : ''}
    </span>
  );

  const actions = (
    <>
      {navigation}
      {link && (
        <Button
          variant="link"
          size="icon-sm"
          aria-label={copied === 'link' ? 'Copied GitHub link' : 'Copy GitHub link'}
          title="Copy GitHub link"
          onClick={() => copyText(link, 'link')}
        >
          {copied === 'link' ? <Check className="text-success" aria-hidden="true" /> : <Link2 aria-hidden="true" />}
        </Button>
      )}
      {link && (
        <Button variant="link" size="icon-sm" href={link} target="_blank" rel="noreferrer" aria-label="Open on GitHub" title="Open on GitHub">
          <ExternalLink aria-hidden="true" />
        </Button>
      )}
    </>
  );

  return (
    <ArtifactPanel
      ref={setSectionRef}
      title={basename(path)}
      description={description}
      actions={actions}
      onClose={onClose}
      autoFocus={autoFocus}
      flush
      className={CODE_BODY}
      {...panelProps}
    >
      {/* Pinned to the top-left of the scrolling body, so it never scrolls away horizontally or vertically. */}
      <div ref={toolbarRef} className="sticky left-0 top-0 z-10 space-y-2 border-b border-line bg-surface px-3 py-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            {active ? (
              <button
                type="button"
                onClick={() => { setSelection(null); scrollToLine(active.start); }}
                className="rounded-full focus-visible:outline-offset-1"
                title="Scroll to the cited lines"
              >
                <Badge tone={beyondEnd ? 'warning' : 'accent'}>Cited lines</Badge>
              </button>
            ) : null}
            {allRanges.length > 1 && (
              <span className="inline-flex items-center rounded-full border border-line bg-surface-2">
                <button type="button" onClick={() => step(-1)} className="rounded-l-full p-1 text-ink-3 hover:text-ink" aria-label="Previous cited range">
                  <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
                <span className="px-0.5 text-[11px] font-medium tabular-nums text-ink-2">{activeIndex + 1}/{allRanges.length}</span>
                <button type="button" onClick={() => step(1)} className="rounded-r-full p-1 text-ink-3 hover:text-ink" aria-label="Next cited range">
                  <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </span>
            )}
            {selection && (
              <span className="inline-flex items-center gap-0.5 rounded-full bg-info-soft py-0.5 pl-2 pr-0.5 text-[11px] font-medium text-info">
                Selected {rangeText(selection.start, selection.end)}
                <button type="button" onClick={() => setSelection(null)} className="rounded-full p-0.5 hover:bg-info/15" aria-label="Clear line selection">
                  <X className="h-3 w-3" aria-hidden="true" />
                </button>
              </span>
            )}
            {data?.source === 'github' && <Badge tone="neutral" title={data.ref ? `Commit ${data.ref}` : undefined}>GitHub @ {shortSha(data.ref) || 'HEAD'}</Badge>}
            {data?.source === 'index' && <Badge tone="warning">From the index</Badge>}
            {data && totalLines ? <span className="text-[11px] text-ink-3">{totalLines.toLocaleString()} lines</span> : null}
          </div>
          <div className="flex shrink-0 items-center gap-0.5">
            {data && lines.length > 0 && (
              <form onSubmit={goToLine} className="mr-1 flex items-center">
                <label htmlFor={lineInputId} className="sr-only">Go to line</label>
                <input
                  id={lineInputId}
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  value={lineInput}
                  onChange={(event) => { setLineInput(event.target.value.replace(/[^0-9]/g, '')); setLineError(''); }}
                  placeholder="Go to line"
                  aria-invalid={Boolean(lineError)}
                  aria-describedby={lineError ? `${lineInputId}-error` : undefined}
                  className="h-8 w-[5.75rem] rounded-lg border border-line bg-surface-2 px-2 font-mono text-[12px] text-ink outline-none placeholder:font-sans placeholder:text-ink-3 focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/25 aria-[invalid=true]:border-danger"
                />
              </form>
            )}
            <IconButton
              label={copied === 'path' ? 'Copied path and lines' : 'Copy path and lines'}
              onClick={() => copyText(linkStart ? `${path}:L${linkStart}-L${linkEnd || linkStart}` : path, 'path')}
            >
              {copied === 'path' ? <Check className="h-4 w-4 text-success" aria-hidden="true" /> : <Copy className="h-4 w-4" aria-hidden="true" />}
            </IconButton>
          </div>
        </div>
        {lineError && <p id={`${lineInputId}-error`} className="text-[12px] text-danger" role="alert">{lineError}</p>}
        <p className="sr-only" aria-live="polite">
          {copied === 'link' ? 'GitHub link copied.' : copied === 'path' ? 'Path and lines copied.' : ''}
        </p>
      </div>
      {data?.exact === false && (
        <div className="sticky left-0 flex items-start gap-2 border-b border-warning/30 bg-warning-soft px-3 py-2 text-[12.5px] text-ink-2" role="note">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
          <span>Reconstructed from the index: whitespace or skipped sections may differ from the file on GitHub.</span>
        </div>
      )}
      {truncated && (
        <div className="sticky left-0 flex items-start gap-2 border-b border-warning/30 bg-warning-soft px-3 py-2 text-[12.5px] text-ink-2" role="note">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
          <span>
            This file is large: showing the first {lines.length.toLocaleString()} of {totalLines.toLocaleString()} lines.
            {beyondShown ? ' The cited lines are further down; open the file on GitHub to see them.' : ''}
            {beyondShown && link ? <> <a href={link} target="_blank" rel="noreferrer" className="font-medium text-accent-strong underline underline-offset-2">Open lines {rangeText(active.start, active.end)} on GitHub</a></> : null}
          </span>
        </div>
      )}
      {beyondEnd && (
        <div className="sticky left-0 flex items-start gap-2 border-b border-warning/30 bg-warning-soft px-3 py-2 text-[12.5px] text-ink-2" role="note">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
          <span>The cited lines are past the end of this file, which has {totalLines.toLocaleString()} line{totalLines === 1 ? '' : 's'}. The reference may be out of date.</span>
        </div>
      )}
      {entry?.loading && (
        <div className="space-y-2 p-4" role="status" aria-label={`Loading ${basename(path)}`}>
          {Array.from({ length: 12 }, (_, index) => <div key={index} className="h-3 rounded bg-code-line/70" style={{ width: `${40 + ((index * 37) % 50)}%` }} />)}
        </div>
      )}
      {entry?.error && (
        <div className="m-4 rounded-xl border border-danger/30 bg-danger-soft p-3 text-sm text-danger" role="alert">
          <p className="flex items-start gap-2"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> {entry.error}</p>
          <div className="mt-3 flex flex-wrap gap-2 pl-6">
            <button
              type="button"
              onClick={() => void loadFile(repoName, path)}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-line bg-surface px-3 text-[12.5px] font-semibold text-ink hover:bg-surface-2"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" /> Try again
            </button>
            {link && (
              <a href={link} target="_blank" rel="noreferrer" className="inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-[12.5px] font-medium text-ink-2 hover:bg-surface hover:text-ink">
                <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" /> View on GitHub
              </a>
            )}
          </div>
        </div>
      )}
      {data && (
        <table
          ref={tableRef}
          className="code-tokens w-full border-collapse font-mono text-[12.5px] leading-5"
          style={virtual ? { minWidth: `calc(${widestLine}ch + 6rem)` } : undefined}
        >
          <tbody>
            {first > 0 && <tr aria-hidden="true" style={{ height: first * ROW_HEIGHT }}><td colSpan={2} /></tr>}
            {lines.slice(first, last).map((html, offset) => {
              const number = first + offset + 1;
              return (
                <CodeRow
                  key={number}
                  number={number}
                  html={html}
                  cited={inside(number, active)}
                  other={otherRanges.some((range) => inside(number, range))}
                  selected={inside(number, selection)}
                  onSelectLine={selectLine}
                />
              );
            })}
            {last < lines.length && <tr aria-hidden="true" style={{ height: (lines.length - last) * ROW_HEIGHT }}><td colSpan={2} /></tr>}
          </tbody>
        </table>
      )}
    </ArtifactPanel>
  );
};

export default FileViewer;
