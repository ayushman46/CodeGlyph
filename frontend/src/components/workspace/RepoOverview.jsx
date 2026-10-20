import { ArrowRight, ChevronRight, ExternalLink, FileCode2, Folder, FolderOpen, PieChart, Search, Sparkles, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { errorMessage, searchRepository } from '../../api/client';
import { parseRepository } from '../../lib/citations';
import { formatBytes, formatCount, relativeTime, shortSha } from '../../lib/format';
import { formatExact } from '../../lib/phase';
import useStore from '../../store/useStore';
import ContextMeter, { filesLabels } from '../ai/ContextMeter';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import ProgressBar from '../ui/ProgressBar';
import Spinner from '../ui/Spinner';

const LANGUAGE_COLORS = ['#e4561b', '#3178c6', '#f1c40f', '#2ea44f', '#8957e5', '#db61a2', '#0ea5e9', '#a3a3a3'];
const SEARCH_LIMIT = 25;
export const CODE_SEARCH_INPUT_ID = 'code-search-input';

const TreeNode = ({ node, depth = 0 }) => {
  const [open, setOpen] = useState(depth === 0 && node.files > 20);
  const hasChildren = node.children?.length > 0;
  const name = node.path.split('/').pop();
  const indent = { paddingLeft: `${depth * 16 + 6}px` };
  const content = (
    <>
      <ChevronRight className={`h-3.5 w-3.5 shrink-0 text-ink-3 transition-transform ${open ? 'rotate-90' : ''} ${hasChildren ? '' : 'opacity-0'}`} aria-hidden="true" />
      {open ? <FolderOpen className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" /> : <Folder className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />}
      <span className="truncate font-mono text-[12.5px] text-ink-2">{name}/</span>
      <span className="ml-auto shrink-0 font-mono text-[11px] text-ink-3"><span className="sr-only">, </span>{formatCount(node.files)}<span className="sr-only"> files</span></span>
    </>
  );
  return (
    <li>
      {hasChildren ? (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex w-full items-center gap-1.5 rounded-lg py-1 pr-2 text-left text-[13px] transition-colors hover:bg-surface-2"
          style={indent}
          aria-expanded={open}
        >
          {content}
        </button>
      ) : (
        <div className="flex w-full items-center gap-1.5 py-1 pr-2 text-[13px]" style={indent}>{content}</div>
      )}
      {open && hasChildren && (
        <ul>{node.children.map((child) => <TreeNode key={child.path} node={child} depth={depth + 1} />)}</ul>
      )}
    </li>
  );
};

const CodeSearch = ({ repoName, onOpenFile }) => {
  const [query, setQuery] = useState('');
  const [state, setState] = useState({ loading: false, results: null, error: '' });
  const requestRef = useRef(0);

  useEffect(() => {
    const value = query.trim();
    if (value.length < 2) {
      requestRef.current += 1;
      setState({ loading: false, results: null, error: '' });
      return undefined;
    }
    const revision = ++requestRef.current;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setState((current) => ({ ...current, loading: true, error: '' }));
      try {
        const response = await searchRepository(repoName, value, { signal: controller.signal });
        if (revision === requestRef.current) setState({ loading: false, results: response.data.results || [], error: '' });
      } catch (error) {
        if (error?.code === 'ERR_CANCELED') return;
        if (revision === requestRef.current) setState({ loading: false, results: null, error: errorMessage(error, 'Search failed. Try again in a moment.') });
      }
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query, repoName]);

  const count = state.results?.length ?? 0;
  const summary = state.results
    ? count === 0 ? `No matches for “${query.trim()}”.` : `${count >= SEARCH_LIMIT ? `First ${SEARCH_LIMIT}` : count} matching ${count === 1 ? 'file' : 'files'}`
    : '';

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
          placeholder="Search code: function names, strings, identifiers…"
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
      {!state.results && !state.error && query.trim().length === 1 && (
        <p className="mt-1.5 text-[12px] text-ink-3">Keep typing: searches need at least 2 characters.</p>
      )}
      {state.error && <p className="mt-2 text-[12.5px] text-danger" role="alert">{state.error}</p>}
      {state.results && (
        <div className="mt-2 overflow-hidden rounded-xl border border-line bg-surface">
          {state.results.length === 0 ? (
            <p className="p-4 text-[13px] text-ink-3">No matches in the indexed code. Try a shorter identifier or an exact string, or ask a question instead.</p>
          ) : (
            <>
              <p className="border-b border-line px-3.5 py-2 text-[11.5px] text-ink-3" aria-hidden="true">{summary}{count >= SEARCH_LIMIT ? ' · refine the search to narrow it down' : ''}</p>
              <ul className="scroll-thin max-h-96 divide-y divide-line overflow-y-auto">
                {state.results.map((result) => (
                  <li key={`${result.file_path}-${result.start_line}`}>
                    <button
                      type="button"
                      onClick={() => onOpenFile({ path: result.file_path, start: result.matches?.[0]?.line || result.start_line, end: result.matches?.[0]?.line || result.start_line })}
                      className="w-full px-3.5 py-2.5 text-left transition-colors hover:bg-surface-2 focus-visible:bg-surface-2"
                    >
                      <span className="flex items-center gap-2 font-mono text-[12.5px] text-ink">
                        <FileCode2 className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />
                        <span className="truncate">{result.file_path}</span>
                      </span>
                      {(result.matches || []).map((match) => (
                        <span key={match.line} className="mt-1 flex gap-3 font-mono text-[11.5px] text-ink-3">
                          <span className="w-10 shrink-0 text-right">{match.line}</span>
                          <span className="truncate text-ink-2">{match.text.trim()}</span>
                        </span>
                      ))}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
};

const CoverageCard = ({ repo, onOpenCoverage }) => {
  const selection = repo.selection || {};
  const seen = Number(repo.total_seen_files) || 0;
  if (!seen) return null;
  const indexed = Number(repo.indexed_files) || 0;
  const share = Math.round((indexed / seen) * 1000) / 10;
  const topReasons = Object.entries(repo.excluded_reasons || {}).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([reason]) => reason.replaceAll('_', ' '));
  return (
    <section className="rounded-2xl border border-line bg-surface p-4" aria-labelledby="coverage-card-title">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id="coverage-card-title" className="text-[13px] font-semibold text-ink">Coverage</h2>
        {selection.budget_omitted_files > 0 && <Badge tone="info">Importance budget</Badge>}
      </div>
      <div className="mb-1.5 flex items-baseline justify-between text-[12.5px]">
        <span className="text-ink-2">{share}% of files searchable</span>
        <span className="font-mono text-[11.5px] text-ink-3">{formatCount(indexed)} / {formatCount(seen)}</span>
      </div>
      <ProgressBar value={share} tone="success" label="Share of files indexed" />
      <p className="mt-2.5 text-[12.5px] leading-relaxed text-ink-3">
        {repo.excluded_files > 0
          ? <>{formatCount(repo.excluded_files)} skipped{topReasons.length ? `, mostly ${topReasons.join(' and ')}` : ''}.</>
          : 'Nothing was skipped.'}
        {selection.budget_omitted_files > 0 && <> {formatCount(selection.budget_omitted_files)} lower-priority files were left out{selection.quota_limited ? ' to fit your plan' : ''}.</>}
      </p>
      {onOpenCoverage && (
        <Button className="mt-3 -ml-1" size="xs" variant="ghost" icon={PieChart} iconRight={ArrowRight} onClick={onOpenCoverage}>View coverage report</Button>
      )}
    </section>
  );
};

/**
 * Files indexed against the indexing file budget (the selector's
 * `selection_budget.max_files`), as a Context Meter. Only rendered when the
 * snapshot records that budget; nothing here is estimated.
 */
const FileBudgetMeter = ({ indexed, budget, selection }) => {
  const omitted = Number(selection.budget_omitted_files) || 0;
  const note = (
    <>
      Each repository indexes up to {formatExact(budget)} files, most important first.
      {omitted > 0
        ? ` ${formatExact(omitted)} lower-priority ${omitted === 1 ? 'file was' : 'files were'} left out ${selection.quota_limited ? 'to fit your plan' : 'by the indexing budget'}; the coverage report lists them.`
        : ' Nothing was left out by the budget.'}
    </>
  );
  // Sits in the tile's value row: the 24px trigger overlaps the padding so the tile keeps its height.
  return <ContextMeter used={Number(indexed) || 0} limit={budget} labels={filesLabels} note={note} className="-my-0.5 -me-2" />;
};

/** Deterministic repository tour: stats, languages, folders, starters, search. */
const RepoOverview = ({ repo, onAsk, compact = false, onOpenCoverage, onShowOverview }) => {
  const loadOverview = useStore((state) => state.loadOverview);
  const overview = useStore((state) => state.overviews[repo.repo_name]);
  const openFile = useStore((state) => state.openFile);

  useEffect(() => {
    if (repo.queryable) void loadOverview(repo.repo_name);
  }, [repo.repo_name, repo.queryable, repo.is_indexing, loadOverview]);

  const data = overview?.data;
  const languages = data?.languages || [];
  const selection = repo.selection || {};
  const repository = parseRepository(repo.github_url);
  const commitUrl = repository && repo.commit_sha ? `https://github.com/${repository.fullName}/commit/${repo.commit_sha}` : null;
  const questions = data?.suggested_questions || [];
  const indexedFiles = repo.indexed_files || data?.total_files;
  // The selector's per-repository file budget, recorded with the snapshot.
  const fileBudget = Number(selection.selection_budget?.max_files) || 0;

  return (
    <div className="space-y-5">
      {compact && (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] text-ink-3">
          <span>{formatCount(repo.indexed_files || data?.total_files)} files indexed</span>
          {languages[0] && <><span aria-hidden="true">·</span><span>mostly {languages[0].language}</span></>}
          {repo.indexed_at && <><span aria-hidden="true">·</span><span>updated {relativeTime(repo.indexed_at)}</span></>}
          {onShowOverview && (
            <button type="button" onClick={onShowOverview} className="inline-flex items-center gap-1 font-medium text-accent-strong hover:underline">
              Folder map &amp; coverage <ArrowRight className="h-3 w-3" aria-hidden="true" />
            </button>
          )}
        </p>
      )}

      {!compact && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ['Indexed files', formatCount(indexedFiles), fileBudget > 0 && <FileBudgetMeter indexed={indexedFiles} budget={fileBudget} selection={selection} />],
            ['Code sections', formatCount(repo.chunk_count)],
            ['Indexed source', formatBytes(selection.selected_bytes || 0)],
          ].map(([label, value, meter]) => (
            <div key={label} className="rounded-xl border border-line bg-surface px-4 py-3">
              <p className="text-[11.5px] text-ink-3">{label}</p>
              {meter ? (
                <div className="mt-1 flex items-center justify-between gap-1">
                  <p className="min-w-0 truncate font-mono text-[15px] font-semibold text-ink">{value}</p>
                  {meter}
                </div>
              ) : (
                <p className="mt-1 truncate font-mono text-[15px] font-semibold text-ink">{value}</p>
              )}
            </div>
          ))}
          <div className="rounded-xl border border-line bg-surface px-4 py-3">
            <p className="text-[11.5px] text-ink-3">Snapshot{repo.indexed_at ? ` · ${relativeTime(repo.indexed_at)}` : ''}</p>
            {commitUrl ? (
              <a href={commitUrl} target="_blank" rel="noreferrer" className="mt-1 inline-flex max-w-full items-center gap-1 font-mono text-[15px] font-semibold text-ink hover:text-accent-strong" title={`Commit ${repo.commit_sha} on GitHub`}>
                <span className="truncate">{shortSha(repo.commit_sha)}</span> <ExternalLink className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" /><span className="sr-only">(opens GitHub)</span>
              </a>
            ) : <p className="mt-1 font-mono text-[15px] font-semibold text-ink">—</p>}
          </div>
        </div>
      )}

      {!compact && repo.queryable && (
        <section aria-labelledby="code-search-title">
          <h2 id="code-search-title" className="mb-2 text-[12px] font-medium text-ink-3">Code search <span className="font-normal">· exact matches, instant, no AI model</span></h2>
          <CodeSearch repoName={repo.repo_name} onOpenFile={(file) => openFile(file)} />
        </section>
      )}

      {onAsk && (questions.length > 0 || (overview?.loading && !data)) && (
        <section aria-labelledby={`suggested-${compact ? 'compact' : 'full'}`}>
          <h2 id={`suggested-${compact ? 'compact' : 'full'}`} className="mb-2 flex items-center gap-1.5 text-[12px] font-medium text-ink-3"><Sparkles className="h-3.5 w-3.5 text-accent" aria-hidden="true" /> Suggested questions</h2>
          <div className="grid gap-2 sm:grid-cols-2">
            {questions.length ? questions.map((question) => (
              <button
                key={question}
                type="button"
                onClick={() => onAsk(question)}
                className="group flex items-start justify-between gap-2 rounded-xl border border-line bg-surface px-3.5 py-2.5 text-left text-[13px] leading-snug text-ink-2 transition-colors hover:border-accent/40 hover:bg-accent-soft hover:text-ink"
              >
                <span>{question}</span>
                <ArrowRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true" />
              </button>
            )) : Array.from({ length: 4 }, (_, index) => <div key={index} className="skeleton h-11 rounded-xl" aria-hidden="true" />)}
          </div>
        </section>
      )}

      {compact && repo.queryable && <CodeSearch repoName={repo.repo_name} onOpenFile={(file) => openFile(file)} />}

      {!compact && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <section className="rounded-2xl border border-line bg-surface p-4" aria-labelledby="folder-map-title">
            <div className="mb-2 flex items-center justify-between">
              <h2 id="folder-map-title" className="text-[13px] font-semibold text-ink">Folder map</h2>
              {data?.total_files > 0 && <span className="text-[11px] text-ink-3">{formatCount(data.total_files)} files</span>}
            </div>
            {overview?.loading && !data ? (
              <div className="space-y-2" aria-hidden="true">{Array.from({ length: 7 }, (_, index) => <div key={index} className="skeleton h-5" style={{ width: `${55 + ((index * 23) % 40)}%` }} />)}</div>
            ) : data?.tree?.length ? (
              <ul className="scroll-thin max-h-[26rem] overflow-y-auto">{data.tree.map((node) => <TreeNode key={node.path} node={node} />)}</ul>
            ) : (
              <p className="text-[13px] text-ink-3">{overview?.error || 'No folders yet.'}</p>
            )}
          </section>
          <div className="space-y-4">
            <CoverageCard repo={repo} onOpenCoverage={onOpenCoverage} />
            {languages.length > 0 && (
              <section className="rounded-2xl border border-line bg-surface p-4" aria-labelledby="languages-title">
                <h2 id="languages-title" className="mb-3 text-[13px] font-semibold text-ink">Languages</h2>
                <div className="flex h-2 overflow-hidden rounded-full bg-surface-3" aria-hidden="true">
                  {languages.map((language, index) => (
                    <span key={language.language} style={{ width: `${language.percent}%`, background: LANGUAGE_COLORS[index % LANGUAGE_COLORS.length] }} title={`${language.language} ${language.percent}%`} />
                  ))}
                </div>
                <ul className="mt-3 space-y-1.5">
                  {languages.slice(0, 6).map((language, index) => (
                    <li key={language.language} className="flex items-center gap-2 text-[12.5px]">
                      <span className="h-2 w-2 rounded-full" style={{ background: LANGUAGE_COLORS[index % LANGUAGE_COLORS.length] }} aria-hidden="true" />
                      <span className="text-ink-2">{language.language}</span>
                      <span className="ml-auto font-mono text-[11.5px] text-ink-3">{language.percent}%</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {data?.key_files?.length > 0 && (
              <section className="rounded-2xl border border-line bg-surface p-4" aria-labelledby="key-files-title">
                <h2 id="key-files-title" className="mb-2 text-[13px] font-semibold text-ink">Key files</h2>
                <ul className="space-y-0.5">
                  {data.key_files.map((path) => (
                    <li key={path}>
                      <button type="button" onClick={() => openFile({ path })} className="flex w-full items-center gap-2 rounded-lg px-1.5 py-1 text-left font-mono text-[12.5px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink">
                        <FileCode2 className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" /> <span className="truncate">{path}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default RepoOverview;
