import { ChevronsDownUp, ChevronsUpDown } from 'lucide-react';
import { memo, useMemo, useState } from 'react';
import { diffLines, toHunks, toRows, toSplitRows, wordDiff } from '../../lib/diff';
import { describeEolChange, exactStats } from '../../lib/eol';
import { planDiffRender } from '../../lib/renderPlan';

const ROW_RENDER_LIMIT = 4000;
// UI diffs use a smaller search budget than the default so a reorder-heavy
// file degrades to a coarser (still exact) diff instead of blocking typing.
export const UI_DIFF_OPTIONS = { maxCost: 5000 };

const EOL_LABEL = { lf: 'LF', crlf: 'CRLF', mixed: 'mixed', none: 'none' };

const eolMessage = (change) => {
  const parts = [];
  if (change.changedLines) {
    parts.push(`Line endings change on ${change.changedLines.toLocaleString()} unchanged-looking line${change.changedLines === 1 ? '' : 's'} (${EOL_LABEL[change.styleBefore]} → ${EOL_LABEL[change.styleAfter]}).`);
  }
  if (change.finalNewlineBefore !== change.finalNewlineAfter) {
    parts.push(change.finalNewlineAfter ? 'Adds a newline at the end of the file.' : 'Removes the newline at the end of the file (\\ No newline at end of file).');
  }
  return parts.join(' ');
};

const tone = {
  add: { row: 'bg-diff-add', gutter: 'bg-diff-gutter-add text-diff-add-ink', text: 'text-code-ink', mark: '+', markClass: 'text-diff-add-ink' },
  del: { row: 'bg-diff-del', gutter: 'bg-diff-gutter-del text-diff-del-ink', text: 'text-code-ink', mark: '−', markClass: 'text-diff-del-ink' },
  context: { row: '', gutter: 'text-code-muted', text: 'text-code-ink/85', mark: ' ', markClass: 'text-code-muted' },
};

/**
 * Pair each run of deletions with the following run of additions so a
 * modified line can show which words changed.
 */
const wordHighlights = (rows) => {
  const highlights = new Map();
  let index = 0;
  while (index < rows.length) {
    if (rows[index].kind !== 'del') {
      index += 1;
      continue;
    }
    const delStart = index;
    while (index < rows.length && rows[index].kind === 'del') index += 1;
    const addStart = index;
    while (index < rows.length && rows[index].kind === 'add') index += 1;
    const pairs = Math.min(addStart - delStart, index - addStart);
    for (let offset = 0; offset < pairs; offset += 1) {
      const oldRow = rows[delStart + offset];
      const newRow = rows[addStart + offset];
      if (oldRow.text.length + newRow.text.length > 2000) continue;
      const segments = wordDiff(oldRow.text, newRow.text);
      // Only highlight when the lines are recognisably related.
      const unchanged = segments.new.filter((segment) => !segment.changed).reduce((sum, segment) => sum + segment.text.length, 0);
      if (unchanged < Math.min(oldRow.text.length, newRow.text.length) * 0.3) continue;
      highlights.set(oldRow, segments.old);
      highlights.set(newRow, segments.new);
    }
  }
  return highlights;
};

const LineText = ({ row, segments }) => {
  if (!segments) return <>{row.text || ' '}</>;
  return segments.map((segment, index) => (
    <span
      key={index}
      className={segment.changed ? (row.kind === 'add' ? 'rounded-[3px] bg-diff-add-strong' : 'rounded-[3px] bg-diff-del-strong') : undefined}
    >
      {segment.text}
    </span>
  ));
};

const UnifiedRow = ({ row, segments }) => {
  const style = tone[row.kind];
  return (
    <tr className={style.row}>
      <td className={`w-12 select-none border-r border-code-line px-2 text-right align-top ${style.gutter}`}>{row.oldNo ?? ''}</td>
      <td className={`w-12 select-none border-r border-code-line px-2 text-right align-top ${style.gutter}`}>{row.newNo ?? ''}</td>
      <td className={`w-6 select-none text-center align-top ${style.markClass}`}>{style.mark}</td>
      <td className={`whitespace-pre-wrap break-all pr-4 align-top ${style.text}`}><LineText row={row} segments={segments} /></td>
    </tr>
  );
};

const SplitCellView = ({ cell, segments }) => {
  if (!cell) return <><td className="w-12 border-r border-code-line bg-code-2/60" /><td className="bg-code-2/60" /></>;
  const style = tone[cell.kind];
  return (
    <>
      <td className={`w-12 select-none border-r border-code-line px-2 text-right align-top ${style.gutter}`}>{cell.no ?? ''}</td>
      <td className={`whitespace-pre-wrap break-all px-3 align-top ${style.row} ${style.text}`}>
        <LineText row={{ kind: cell.kind, text: cell.text }} segments={segments} />
      </td>
    </>
  );
};

const Expander = ({ count, onExpand }) => (
  <tr>
    <td colSpan={4} className="border-y border-code-line bg-code-2 p-0">
      <button type="button" onClick={onExpand} className="flex w-full items-center gap-2 px-4 py-1 text-left text-[11.5px] text-code-muted transition-colors hover:bg-code-line hover:text-code-ink">
        <ChevronsUpDown className="h-3.5 w-3.5" aria-hidden="true" />
        Show {count.toLocaleString()} unchanged line{count === 1 ? '' : 's'}
      </button>
    </td>
  </tr>
);

const HunkHeader = ({ header }) => (
  <tr>
    <td colSpan={4} className="border-y border-code-line bg-[color-mix(in_oklab,var(--color-info)_14%,var(--color-code))] px-4 py-1 font-mono text-[11.5px] text-code-ink/75">{header}</td>
  </tr>
);

/** GitHub-style diff of one file: unified or split, with word highlights. */
const DiffView = ({ before = '', after = '', view = 'unified', className = '' }) => {
  const [expanded, setExpanded] = useState(() => new Set());
  const [showAll, setShowAll] = useState(false);
  const { items, stats, highlights, totalRows, eolChange, hasLineChanges } = useMemo(() => {
    const ops = diffLines(before ?? '', after ?? '', UI_DIFF_OPTIONS);
    const rows = toRows(ops);
    return {
      items: toHunks(rows, 3),
      // Counted like git: line-ending and final-newline changes included.
      stats: exactStats(before ?? '', after ?? '', UI_DIFF_OPTIONS),
      highlights: wordHighlights(rows),
      totalRows: rows.length,
      eolChange: describeEolChange(before ?? '', after ?? ''),
      hasLineChanges: rows.some((row) => row.kind !== 'context'),
    };
  }, [before, after]);

  if (!hasLineChanges && !eolChange && (before ?? '') === (after ?? '')) {
    return <div className={`rounded-xl border border-code-line bg-code px-4 py-6 text-center text-sm text-code-muted ${className}`}>No changes in this file.</div>;
  }
  if (!hasLineChanges) {
    return (
      <div className={`rounded-xl border border-warning/40 bg-code px-4 py-5 text-center text-sm text-code-ink ${className}`} role="note">
        {eolChange ? eolMessage(eolChange) : 'Only invisible characters change in this file.'}
        <span className="mt-1 block font-mono text-[11.5px] text-code-muted">
          <span className="text-diff-add-ink">+{stats.additions}</span>{' '}<span className="text-diff-del-ink">−{stats.deletions}</span>
        </span>
      </div>
    );
  }

  const plan = planDiffRender(items, { expanded, showAll, limit: ROW_RENDER_LIMIT });
  const body = [];
  for (const { index: itemIndex, item, collapsed } of plan.entries) {
    if (collapsed) {
      body.push(<Expander key={`c${itemIndex}`} count={item.count} onExpand={() => setExpanded((current) => new Set(current).add(itemIndex))} />);
      continue;
    }
    if (item.kind === 'hunk') body.push(<HunkHeader key={`h${itemIndex}`} header={item.header} />);
    if (view === 'split') {
      // Line numbers are unique per side, so map them back to the unified
      // rows that carry the word highlights in O(n).
      const deletedByLine = new Map();
      const addedByLine = new Map();
      item.rows.forEach((row) => {
        if (row.kind === 'del') deletedByLine.set(row.oldNo, row);
        else if (row.kind === 'add') addedByLine.set(row.newNo, row);
      });
      toSplitRows(item.rows).forEach((pair, pairIndex) => {
        const leftSegments = pair.left?.kind === 'del' ? highlights.get(deletedByLine.get(pair.left.no)) : null;
        const rightSegments = pair.right?.kind === 'add' ? highlights.get(addedByLine.get(pair.right.no)) : null;
        body.push(
          <tr key={`s${itemIndex}-${pairIndex}`}>
            <SplitCellView cell={pair.left} segments={leftSegments} />
            <SplitCellView cell={pair.right} segments={rightSegments} />
          </tr>,
        );
      });
    } else {
      item.rows.forEach((row, rowIndex) => {
        body.push(<UnifiedRow key={`u${itemIndex}-${rowIndex}`} row={row} segments={highlights.get(row)} />);
      });
    }
  }
  const truncated = !showAll && plan.truncated;
  const anyCollapsed = items.some((item, index) => item.kind === 'collapsed' && !expanded.has(index));

  return (
    <div className={`overflow-hidden rounded-xl border border-code-line bg-code ${className}`}>
      <div className="flex items-center justify-between gap-3 border-b border-code-line bg-code-2 px-3 py-1.5 text-[11.5px]">
        <span className="font-mono">
          <span className="text-diff-add-ink">+{stats.additions}</span>{' '}
          <span className="text-diff-del-ink">−{stats.deletions}</span>
        </span>
        {eolChange && <span className="min-w-0 flex-1 truncate text-[#f6c177]" title={eolMessage(eolChange)}>{eolMessage(eolChange)}</span>}
        <button
          type="button"
          onClick={() => setExpanded(anyCollapsed ? new Set(items.map((_, index) => index)) : new Set())}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-code-muted transition-colors hover:bg-code-line hover:text-code-ink"
        >
          {anyCollapsed ? <ChevronsUpDown className="h-3.5 w-3.5" /> : <ChevronsDownUp className="h-3.5 w-3.5" />}
          {anyCollapsed ? 'Expand all' : 'Collapse'}
        </button>
      </div>
      <div className="code-scroll max-h-[min(70vh,44rem)] overflow-auto">
        <table className={`w-full border-collapse font-mono text-[12px] leading-[1.65] ${view === 'split' ? 'table-fixed' : ''}`}>
          {view === 'split' && (
            <colgroup><col className="w-12" /><col /><col className="w-12" /><col /></colgroup>
          )}
          <tbody>{body}</tbody>
        </table>
        {truncated && (
          <button type="button" onClick={() => setShowAll(true)} className="w-full border-t border-code-line bg-code-2 py-2 text-[12px] text-code-muted hover:text-code-ink">
            Large diff — show all {totalRows.toLocaleString()} lines
          </button>
        )}
      </div>
    </div>
  );
};

export default memo(DiffView);
