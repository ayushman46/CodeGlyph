import { ChevronRight, Search } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { enclosingSymbols, symbolLabel } from '../../lib/codeNav';

/**
 * "class Group › def parse_args": the declarations that contain `line`,
 * each a button that jumps to its declaring line. Nothing when the line is
 * outside every declaration.
 */
export const SymbolBreadcrumb = ({ outline, line, onJump }) => {
  const chain = useMemo(() => enclosingSymbols(outline, line), [outline, line]);
  if (!chain.length) return null;
  return (
    <nav aria-label={`Line ${line} is inside`} className="flex min-w-0 items-center gap-0.5 overflow-hidden font-mono text-[11.5px] text-ink-3">
      {chain.map((entry, index) => (
        <span key={`${entry.line}-${entry.name}`} className="flex min-w-0 items-center gap-0.5">
          {index > 0 && <ChevronRight className="h-3 w-3 shrink-0" aria-hidden="true" />}
          <button
            type="button"
            onClick={() => onJump(entry.line)}
            className="min-w-0 truncate rounded px-1 py-0.5 hover:bg-surface-2 hover:text-ink"
            title={`Go to line ${entry.line}`}
          >
            {symbolLabel(entry)}
          </button>
        </span>
      ))}
    </nav>
  );
};

/**
 * The file's declarations (Markdown: its headings), indented by nesting.
 * A filter narrows long outlines; choosing an entry jumps to its line. The
 * entry containing `line` is marked as current.
 */
export const OutlineList = ({ outline, line, onJump, id }) => {
  const [filter, setFilter] = useState('');
  const inputId = useId();
  const listRef = useRef(null);
  const current = useMemo(() => enclosingSymbols(outline, line).at(-1) || null, [outline, line]);
  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? outline.filter((entry) => entry.name.toLowerCase().includes(needle)) : outline;
  }, [outline, filter]);

  useEffect(() => {
    listRef.current?.querySelector('[aria-current="true"]')?.scrollIntoView?.({ block: 'nearest' });
  }, []);

  return (
    <div id={id} className="border-t border-line pt-2">
      {outline.length > 12 && (
        <label htmlFor={inputId} className="relative mb-1.5 block">
          <span className="sr-only">Filter the outline</span>
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" aria-hidden="true" />
          <input
            id={inputId}
            type="search"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filter symbols"
            autoComplete="off"
            spellCheck={false}
            className="h-8 w-full rounded-lg border border-line bg-surface-2 pl-7 pr-2 text-[12px] text-ink outline-none placeholder:text-ink-3 focus-visible:border-accent"
          />
        </label>
      )}
      <ul ref={listRef} className="scroll-thin max-h-56 overflow-y-auto" aria-label="Outline">
        {shown.length === 0 && <li className="px-2 py-1.5 text-[12px] text-ink-3">No symbol matches “{filter.trim()}”.</li>}
        {shown.map((entry) => {
          const isCurrent = current === entry;
          return (
            <li key={`${entry.line}-${entry.name}`}>
              <button
                type="button"
                onClick={() => onJump(entry.line)}
                aria-current={isCurrent ? 'true' : undefined}
                className={`flex w-full items-baseline gap-2 rounded-md py-1 pr-2 text-left font-mono text-[12px] hover:bg-surface-2 ${isCurrent ? 'bg-accent-soft text-ink' : 'text-ink-2'}`}
                style={{ paddingLeft: `${8 + Math.min(entry.depth || 0, 6) * 14}px` }}
              >
                <span className="min-w-0 flex-1 truncate">{symbolLabel(entry)}</span>
                <span className="shrink-0 text-[11px] text-ink-3">{entry.line}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
};
