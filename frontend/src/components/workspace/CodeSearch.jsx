import { ChevronRight, FileCode2, Search, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { errorMessage, searchRepository } from '../../api/client';
import { highlightParts, searchSummary } from '../../lib/codeNav';
import Spinner from '../ui/Spinner';

export const CODE_SEARCH_INPUT_ID = 'code-search-input';
// Files listed open at first, and lines shown per file before "Show all".
const OPEN_FILES = 6;
const LINES_SHOWN = 8;

const MatchText = ({ text, spans }) => highlightParts(text, spans).map((part, index) => {
  // The parts of one line never reorder, so their position is a stable key.
  const key = `part-${index}`;
  return part.match
    ? <mark key={key} className="rounded-[3px] bg-[var(--color-highlight)] px-px text-ink">{part.text}</mark>
    : <span key={key}>{part.text}</span>;
});

/** One file: its path and match count (a disclosure), then each matching line as its own link. */
const FileResult = ({ result, defaultOpen, onOpenLine }) => {
  const [open, setOpen] = useState(defaultOpen);
  const [all, setAll] = useState(false);
  const matches = result.matches || [];
  const count = result.match_count ?? matches.length;
  const visible = all ? matches : matches.slice(0, LINES_SHOWN);
  const slash = result.file_path.lastIndexOf('/');
  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-surface-2 focus-visible:bg-surface-2"
        title={result.file_path}
      >
        <ChevronRight className={`h-3.5 w-3.5 shrink-0 text-ink-3 transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
        <FileCode2 className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">
          {slash > 0 && <span className="text-ink-3">{result.file_path.slice(0, slash + 1)}</span>}
          <span className="font-medium text-ink">{result.file_path.slice(slash + 1)}</span>
        </span>
        {result.definitions > 0 && <span className="hidden shrink-0 rounded-full bg-accent-soft px-1.5 py-px text-[10.5px] font-medium text-accent-strong sm:inline">defined here</span>}
        <span className="shrink-0 font-mono text-[11px] text-ink-3">{count} {count === 1 ? 'line' : 'lines'}</span>
      </button>
      {open && (
        <ul className="pb-1.5">
          {visible.map((match) => (
            <li key={match.line}>
              <button
                type="button"
                onClick={() => onOpenLine(result.file_path, match.line)}
                className="flex w-full items-baseline gap-3 py-[3px] pl-9 pr-3 text-left font-mono text-[11.5px] transition-colors hover:bg-surface-2 focus-visible:bg-surface-2"
                title={`Open ${result.file_path} at line ${match.line}`}
                data-line={match.line}
              >
                <span className="w-10 shrink-0 text-right text-ink-3"><span className="sr-only">Line </span>{match.line}</span>
                <span className="min-w-0 flex-1 truncate whitespace-pre text-ink-2"><MatchText text={match.text} spans={match.spans} /></span>
                {match.definition && <span className="shrink-0 font-sans text-[10.5px] text-accent-strong">definition</span>}
              </button>
            </li>
          ))}
          {!all && matches.length > LINES_SHOWN && (
            <li>
              <button type="button" onClick={() => setAll(true)} className="ml-9 mt-0.5 rounded px-1 text-[11.5px] font-medium text-accent-strong hover:underline">
                Show all {matches.length} lines
              </button>
            </li>
          )}
          {(all || matches.length <= LINES_SHOWN) && result.truncated && (
            <li className="ml-9 mt-0.5 text-[11.5px] text-ink-3">
              {count > matches.length ? `Listing the first ${matches.length} of ${count} lines. ` : 'This file has more matches than were read. '}
              <button type="button" onClick={() => onOpenLine(result.file_path, null)} className="font-medium text-accent-strong hover:underline">Open the file</button>
            </li>
          )}
        </ul>
      )}
    </li>
  );
};

/**
 * Instant, exact code search (no model): every line containing the text,
 * one disclosure per file, each line opening the viewer at that line.
 * Quoted phrases match exactly; several words match where they appear
 * together, else lines that contain all of them.
 */
const CodeSearch = ({ repoName, onOpenFile }) => {
  const [query, setQuery] = useState('');
  const [state, setState] = useState({ loading: false, response: null, error: '' });
  const requestRef = useRef(0);

  useEffect(() => {
    const value = query.trim();
    if (value.length < 2) {
      requestRef.current += 1;
      setState({ loading: false, response: null, error: '' });
      return undefined;
    }
    const revision = ++requestRef.current;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setState((current) => ({ ...current, loading: true, error: '' }));
      try {
        const response = await searchRepository(repoName, value, { signal: controller.signal });
        if (revision === requestRef.current) setState({ loading: false, response: { ...response.data, query: value }, error: '' });
      } catch (error) {
        if (error?.code === 'ERR_CANCELED') return;
        if (revision === requestRef.current) setState({ loading: false, response: null, error: errorMessage(error, 'Search failed. Try again in a moment.') });
      }
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query, repoName]);

  const response = state.response;
  const results = response?.results || null;
  const lines = response?.line_count ?? (results || []).reduce((sum, result) => sum + (result.match_count ?? result.matches?.length ?? 0), 0);
  const summary = results
    ? results.length === 0 ? `No matches for “${response.query}”.` : searchSummary({ files: results.length, lines, more: Boolean(response.more_files) })
    : '';
  const openLine = (path, line) => onOpenFile({ path, start: line, end: line, from: 'search' });

  return (
    <div>
      <label className="relative block">
        <span className="sr-only">Search code</span>
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" aria-hidden="true" />
        <input
          id={CODE_SEARCH_INPUT_ID}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Escape' && query) { event.stopPropagation(); setQuery(''); } }}
          placeholder="Search code: names, strings, “exact phrases”…"
          autoComplete="off"
          spellCheck={false}
          className="h-10 w-full rounded-xl border border-line bg-surface pl-9 pr-9 text-[13.5px] text-ink outline-none transition-colors placeholder:text-ink-3 focus:border-accent [&::-webkit-search-cancel-button]:hidden"
        />
        {state.loading ? (
          <Spinner className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
        ) : query && (
          <button type="button" onClick={() => setQuery('')} className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1 text-ink-3 hover:bg-surface-2 hover:text-ink" aria-label="Clear search">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </label>
      <p className="sr-only" role="status" aria-live="polite">{state.loading ? 'Searching' : summary}</p>
      {!results && !state.error && query.trim().length === 1 && (
        <p className="mt-1.5 text-[12px] text-ink-3">Keep typing: searches need at least 2 characters.</p>
      )}
      {state.error && <p className="mt-2 text-[12.5px] text-danger" role="alert">{state.error}</p>}
      {results && (
        <div className="mt-2 overflow-hidden rounded-xl border border-line bg-surface">
          {results.length === 0 ? (
            <p className="p-4 text-[13px] text-ink-3">{response.message || 'No line in the indexed code contains this text. Try a single name, fewer words, or ask a question instead.'}</p>
          ) : (
            <>
              <div className="border-b border-line px-3.5 py-2 text-[11.5px] text-ink-3">
                <p aria-hidden="true" data-testid="code-search-summary">{summary}{response.more_files ? ' · refine the search to narrow it down' : ''}</p>
                {response.mode === 'all_words' && (
                  <p className="mt-0.5">No line has these words together, so these lines contain each of them.</p>
                )}
              </div>
              <ul className="scroll-thin max-h-[28rem] divide-y divide-line overflow-y-auto" aria-label="Matching files">
                {results.map((result, index) => (
                  <FileResult key={`${response.query}:${result.file_path}`} result={result} defaultOpen={index < OPEN_FILES} onOpenLine={openLine} />
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
};

export default CodeSearch;
