import {
  AlertTriangle, ArrowLeft, Check, ChevronRight, Copy, FileCode2, PanelRight, RefreshCw, Search, Square,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { errorMessage, getChangeImpact, getImpactSuggestions, getRepositoryCoverage } from '../../api/client';
import { formatBytes, formatCount, shownLabel } from '../../lib/format';
import {
  MAX_IMPACT_DEPTH, depthLabel, examplePath, groupDependents, groupTestFunctions, impactMarkdown, testMatchLabel,
} from '../../lib/impact';
import useStore from '../../store/useStore';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import ProgressBar from '../ui/ProgressBar';
import Spinner from '../ui/Spinner';
import FileViewer from './FileViewer';

const REASON_LABELS = {
  hidden: 'Hidden files',
  symlink: 'Symbolic links',
  lockfile: 'Lockfiles',
  minified: 'Minified files',
  build_artifact: 'Build artifacts',
  translations: 'Non-English translations',
  file_size_limit: 'Over the per-file size limit',
  unsupported_format: 'Binary or unsupported formats',
  binary_or_invalid_text: 'Binary or invalid text',
  repository_budget: 'Lower priority (indexing budget)',
  chunk_budget: 'Lower priority (section budget)',
  partially_indexed: 'Partially indexed (budget reached mid-file)',
  empty_or_unreadable: 'Empty or unreadable',
  unsafe_path: 'Unsafe archive paths',
  chunking_failed: 'Could not be read',
};

const REASON_DETAILS = {
  hidden: 'Dotfiles and hidden folders, which often hold secrets or editor state. CI folders such as .github are kept.',
  symlink: 'Links are not followed, so code is never indexed twice or from outside the repository.',
  lockfile: 'Dependency lockfiles are long and machine-written.',
  minified: 'Bundled or minified code has no useful line structure.',
  build_artifact: 'Generated output such as dist/, build/ or .next/.',
  translations: 'Locale files in other languages; English copies are kept.',
  file_size_limit: 'Very large single files.',
  unsupported_format: 'Images, fonts, archives and other non-text files.',
  binary_or_invalid_text: 'Files that are not valid text.',
  repository_budget: 'Ranked below the most important files for this repository’s size budget.',
  chunk_budget: 'Ranked below the most important files for the section budget.',
  partially_indexed: 'The beginning of the file is searchable; the rest was cut off by the budget.',
  empty_or_unreadable: 'Empty files, or files that could not be decoded.',
  unsafe_path: 'Archive entries with paths that could escape the repository.',
  chunking_failed: 'The file could not be split into sections.',
};

const renameProblem = (value, current) => {
  const name = value.split(/\s+/).filter(Boolean).join(' ');
  if (!name) return 'Enter a name.';
  if (/[/\\?#%]/.test(name)) return 'Names cannot contain /, \\, ?, # or %.';
  // eslint-disable-next-line no-control-regex
  if (/^\.+$/.test(name) || /[\u0000-\u001f\u007f]/.test(name)) return 'Names cannot be only dots or contain control characters.';
  if (name === current) return 'same';
  return '';
};

/** The repository as it is now (dialogs outlive the snapshot they opened with). */
const useLiveRepo = (repo) => useStore((state) => state.repos.find((item) => item.id === repo.id)) || repo;

const RenameDialog = ({ repo, onClose }) => {
  const renameRepo = useStore((state) => state.renameRepo);
  const navigate = useNavigate();
  const inputRef = useRef(null);
  const [name, setName] = useState(repo.repo_name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const problem = renameProblem(name, repo.repo_name);

  useEffect(() => {
    const timer = window.setTimeout(() => inputRef.current?.select(), 20);
    return () => window.clearTimeout(timer);
  }, []);

  const submit = async (event) => {
    event.preventDefault();
    if (problem === 'same') return onClose();
    if (problem) return setError(problem);
    setBusy(true);
    setError('');
    try {
      const next = await renameRepo(repo, name.trim());
      onClose();
      navigate(`/app/r/${encodeURIComponent(next)}`, { replace: true });
    } catch (renameError) {
      setError(errorMessage(renameError, 'Could not rename this repository.'));
    } finally {
      setBusy(false);
    }
    return undefined;
  };
  const shown = error || (problem && problem !== 'same' && name.trim() ? problem : '');
  return (
    <Modal open onClose={onClose} busy={busy} size="sm" title="Rename repository" initialFocusRef={inputRef}
      description="Changes the name in your workspace and its link. The GitHub repository is not affected."
      footer={<><Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>Cancel</Button><Button variant="primary" size="sm" loading={busy} disabled={Boolean(problem)} type="submit" form="rename-form">Save</Button></>}>
      <form id="rename-form" onSubmit={submit} noValidate>
        <label htmlFor="rename-input" className="text-[12.5px] font-medium text-ink-2">Repository name</label>
        <input
          id="rename-input"
          ref={inputRef}
          value={name}
          maxLength={200}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => { setName(event.target.value); setError(''); }}
          aria-invalid={shown ? true : undefined}
          aria-describedby="rename-hint"
          className={`mt-1.5 h-10 w-full rounded-xl border bg-surface px-3 text-sm text-ink outline-none focus:border-accent ${shown ? 'border-danger/60' : 'border-line'}`}
        />
        <p id="rename-hint" className={`mt-2 text-[12.5px] ${shown ? 'text-danger' : 'text-ink-3'}`} role={error ? 'alert' : undefined}>
          {shown || <>Link: <span className="font-mono text-ink-2">/app/r/{encodeURIComponent(name.trim() || repo.repo_name)}</span></>}
        </p>
      </form>
    </Modal>
  );
};

const DeleteDialog = ({ repo: snapshot, onClose }) => {
  const repo = useLiveRepo(snapshot);
  const deleteRepo = useStore((state) => state.deleteRepo);
  const cancelRepoIndexing = useStore((state) => state.cancelRepoIndexing);
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState('');
  const indexing = Boolean(repo.is_indexing);
  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      await deleteRepo(repo);
      onClose();
      navigate('/app', { replace: true });
    } catch (deleteError) {
      setError(errorMessage(deleteError, 'Could not delete this repository.'));
    } finally {
      setBusy(false);
    }
  };
  const stop = async () => {
    setStopping(true);
    setError('');
    try {
      await cancelRepoIndexing(repo);
    } catch (stopError) {
      setError(errorMessage(stopError, 'Could not stop indexing.'));
    } finally {
      setStopping(false);
    }
  };
  const facts = [
    repo.queryable && `${formatCount(repo.indexed_files)} indexed files`,
    repo.selection?.selected_bytes > 0 && `${formatBytes(repo.selection.selected_bytes)} of indexed source`,
    'the conversation and saved change proposals',
  ].filter(Boolean);
  return (
    <Modal open onClose={onClose} busy={busy} size="sm" title={`Delete ${repo.repo_name}?`} description="Removes it from your workspace. Your GitHub repository is not affected."
      footer={(
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>Cancel</Button>
          {indexing && <Button variant="secondary" size="sm" icon={Square} loading={stopping} onClick={stop}>Stop indexing</Button>}
          <Button variant="danger" size="sm" loading={busy} disabled={indexing} onClick={confirm}>Delete</Button>
        </>
      )}>
      <div className="space-y-3 text-[13px] leading-relaxed text-ink-2">
        {indexing && (
          <p className="flex gap-2 rounded-xl bg-warning-soft px-3 py-2.5 text-warning">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <span>{repo.message?.startsWith('Stopping') ? 'Stopping indexing… Delete becomes available in a moment.' : 'This repository is being indexed. Stop indexing first, then delete it.'}</span>
          </p>
        )}
        <div>
          <p className="text-ink-3">This permanently removes:</p>
          <ul className="mt-1.5 list-disc space-y-0.5 pl-5 marker:text-ink-3">
            {facts.map((fact) => <li key={fact}>{fact}</li>)}
          </ul>
          <p className="mt-2 text-ink-3">The indexed storage is freed immediately.</p>
        </div>
        {error && <p className="text-danger" role="alert">{error}</p>}
      </div>
    </Modal>
  );
};

const ReindexDialog = ({ repo: snapshot, onClose }) => {
  const repo = useLiveRepo(snapshot);
  const reindexRepo = useStore((state) => state.reindexRepo);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      await reindexRepo(repo);
      onClose();
    } catch (reindexError) {
      setError(errorMessage(reindexError, 'Could not queue a re-index.'));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={onClose} busy={busy} size="sm" title={`Re-index ${repo.repo_name}?`}
      description={`Downloads the latest commit of ${repo.full_name || repo.repo_name} and rebuilds the index.`}
      footer={<><Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>Cancel</Button><Button variant="primary" size="sm" icon={RefreshCw} loading={busy} disabled={repo.is_indexing} onClick={confirm}>Re-index</Button></>}>
      <ul className="list-disc space-y-1.5 pl-5 text-[13px] leading-relaxed text-ink-2 marker:text-ink-3">
        <li>Unchanged files are reused, so a refresh is usually faster than the first run.</li>
        {repo.queryable && <li>You can keep asking questions: answers use the current index until the refresh finishes.</li>}
        <li>You can stop the refresh at any time.</li>
      </ul>
      {repo.is_indexing && <p className="mt-3 text-[13px] text-ink-3">This repository is already being indexed.</p>}
      {error && <p className="mt-3 text-[13px] text-danger" role="alert">{error}</p>}
    </Modal>
  );
};

const StopDialog = ({ repo: snapshot, onClose }) => {
  const repo = useLiveRepo(snapshot);
  const cancelRepoIndexing = useStore((state) => state.cancelRepoIndexing);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      await cancelRepoIndexing(repo);
      onClose();
    } catch (stopError) {
      setError(errorMessage(stopError, 'Could not stop indexing.'));
    } finally {
      setBusy(false);
    }
  };
  // What survives a stop: a refresh falls back to the previous index; a
  // first run (even one that is already keyword-searchable) is discarded.
  const consequence = repo.refreshing
    ? 'Only this refresh is discarded. The previous index stays available, so you can keep asking questions.'
    : repo.queryable
      ? 'This is the first index, so stopping discards it, including the keyword search that is already ready.'
      : 'Nothing has been made searchable yet, so nothing is lost.';
  return (
    <Modal open onClose={onClose} busy={busy} size="sm" title={`Stop indexing ${repo.repo_name}?`} description={consequence}
      footer={<><Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>Keep indexing</Button><Button variant="danger" size="sm" icon={Square} loading={busy} disabled={!repo.is_indexing} onClick={confirm}>Stop indexing</Button></>}>
      <p className="text-[13px] leading-relaxed text-ink-2">You can start it again any time with Re-index.</p>
      {!repo.is_indexing && <p className="mt-2 text-[13px] text-ink-3">Indexing has already finished.</p>}
      {error && <p className="mt-3 text-[13px] text-danger" role="alert">{error}</p>}
    </Modal>
  );
};

const ImpactRow = ({ rowKey, path, line, detail, badge, onOpen }) => (
  <li>
    <button
      type="button"
      data-impact-row={rowKey}
      onClick={onOpen}
      className="group flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-surface-2"
    >
      <FileCode2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-[12.5px] text-ink-2 group-hover:text-ink" title={path}>
          {path}{line ? <span className="text-ink-3">:{line}</span> : null}
        </span>
        {detail && <span className="block truncate text-[11.5px] leading-snug text-ink-3" title={detail}>{detail}</span>}
      </span>
      {badge}
    </button>
  </li>
);

const SectionTitle = ({ children, aside }) => (
  <div className="mb-1 flex items-baseline justify-between gap-3">
    <h3 className="text-[11px] font-semibold uppercase tracking-wider text-ink-3">{children}</h3>
    {aside}
  </div>
);

/**
 * The dependents found at one depth, each opening at its import line. A
 * long indirect level starts folded so the affected tests stay in reach.
 */
const DependentGroup = ({ group, showDepth, onOpen }) => {
  const rows = (
    <ul>
      {group.items.map((node) => (
        <ImpactRow
          key={node.file_path}
          rowKey={`dep:${node.file_path}`}
          path={node.file_path}
          line={node.line_number}
          detail={node.depth > 1 ? `via ${node.via}` : null}
          badge={node.is_test ? <Badge tone="info" className="mt-0.5">test</Badge> : null}
          onOpen={() => onOpen({
            key: `dep:${node.file_path}`, path: node.file_path, start: node.line_number,
            caption: `${node.file_path} imports ${node.via} on line ${node.line_number}.`,
          })}
        />
      ))}
      {group.items.length < group.total && (
        <li className="px-2 pt-1 text-[12px] text-ink-3">Showing {formatCount(group.items.length)} of {formatCount(group.total)}.</li>
      )}
    </ul>
  );
  if (!showDepth) return rows;
  const heading = <>{depthLabel(group.depth)} <span className="font-normal text-ink-3">({formatCount(group.total)})</span></>;
  if (group.depth === 1) return <div><p className="mb-0.5 mt-2 px-2 text-[12px] font-medium text-ink-2">{heading}</p>{rows}</div>;
  return (
    <details className="group/depth mt-1" open={group.total <= 10}>
      <summary className="flex cursor-pointer list-none items-center gap-1 rounded-lg px-2 py-1 text-[12px] font-medium text-ink-2 hover:bg-surface-2 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3.5 w-3.5 text-ink-3 transition-transform group-open/depth:rotate-90" aria-hidden="true" />
        {heading}
      </summary>
      {rows}
    </details>
  );
};

const ImpactDialog = ({ repo, onClose }) => {
  const openFile = useStore((state) => state.openFile);
  // The file open in the viewer is the likely subject of the question.
  const viewerPath = useStore((state) => (state.sources.open ? state.sources.file?.path || '' : ''));
  const rootFiles = useStore((state) => state.overviews[repo.repo_name]?.data?.root_files);
  const [path, setPath] = useState(viewerPath);
  const [symbol, setSymbol] = useState('');
  const [indirect, setIndirect] = useState(false);
  const [state, setState] = useState({ loading: false, result: null, error: '' });
  const [suggestions, setSuggestions] = useState({ files: [], graph: null });
  const [viewing, setViewing] = useState(null);
  const [copy, setCopy] = useState({ status: '', text: '' });
  const inputRef = useRef(null);
  const backRef = useRef(null);
  const bodyRef = useRef(null);
  const returnRow = useRef('');
  const requestId = useRef(0);
  const result = state.result;

  useEffect(() => {
    const controller = new AbortController();
    getImpactSuggestions(repo.repo_name, { signal: controller.signal })
      .then((response) => setSuggestions({ files: response.data?.files || [], graph: response.data?.graph || null }))
      .catch(() => {});
    return () => { controller.abort(); requestId.current = -1; };
  }, [repo.repo_name]);

  useEffect(() => {
    const timer = window.setTimeout(() => inputRef.current?.select(), 20);
    return () => window.clearTimeout(timer);
  }, []);

  // Moving between the report and a file keeps focus where the reader is.
  // The report stays mounted (hidden) while a file is open, so levels the
  // reader unfolded stay open; a row in a folded level is unfolded anyway.
  useEffect(() => {
    if (viewing) {
      backRef.current?.focus();
      return undefined;
    }
    const key = returnRow.current;
    const row = key ? bodyRef.current?.querySelector(`[data-impact-row="${CSS.escape(key)}"]`) : null;
    const fold = row?.closest('details');
    if (fold && !fold.open) fold.open = true;
    row?.focus();
    return undefined;
  }, [viewing]);

  // Escape inside a file goes back to the report; a second Escape closes.
  useEffect(() => {
    if (!viewing) return undefined;
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      event.preventDefault();
      setViewing(null);
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [viewing]);

  const run = async (target = path, options = {}) => {
    const filePath = String(target || '').trim();
    if (!filePath) return;
    const deep = options.indirect ?? indirect;
    const name = (options.symbol ?? symbol).trim();
    const request = ++requestId.current;
    setCopy({ status: '', text: '' });
    setState((current) => ({ ...current, loading: true, error: '' }));
    try {
      const response = await getChangeImpact(repo.repo_name, { filePath, depth: deep ? MAX_IMPACT_DEPTH : 1, symbol: name });
      if (request !== requestId.current) return;
      setState({ loading: false, result: { ...response.data, requestedSymbol: name }, error: '' });
    } catch (error) {
      if (request !== requestId.current) return;
      setState({ loading: false, result: null, error: errorMessage(error, 'Impact analysis failed. Check the file path and try again.') });
    }
  };

  const submit = (event) => {
    event.preventDefault();
    void run();
  };
  const choose = (filePath) => {
    setPath(filePath);
    void run(filePath);
  };
  const setDepth = (deep) => {
    setIndirect(deep);
    if (result?.target_file) void run(result.target_file, { indirect: deep });
  };
  const open = (file) => {
    returnRow.current = file.key;
    setViewing(file);
  };
  const openInPanel = () => {
    openFile({ path: viewing.path, start: viewing.start || null, end: viewing.end || viewing.start || null });
    onClose();
  };
  const copyReport = async () => {
    const text = impactMarkdown(result, { symbol: result.requestedSymbol });
    try {
      await navigator.clipboard.writeText(text);
      setCopy({ status: 'copied', text: '' });
      window.setTimeout(() => setCopy((current) => (current.status === 'copied' ? { status: '', text: '' } : current)), 1800);
    } catch {
      // No clipboard (permissions, insecure context): show the text to copy by hand.
      setCopy({ status: 'manual', text });
    }
  };

  const groups = useMemo(() => groupDependents(result), [result]);
  const testFunctions = useMemo(() => groupTestFunctions(result?.affected_tests?.functions || []), [result]);
  const graph = result?.graph || suggestions.graph;
  const example = examplePath(suggestions.files, rootFiles?.[0]);
  const tests = result?.affected_tests;
  const requestedSymbol = result?.requestedSymbol || '';
  const totalDependents = groups.reduce((sum, group) => sum + group.total, 0);
  // Tests further away than the chosen depth (only reported for direct analysis).
  const moreTests = result?.depth === 1 ? Number(tests?.indirect_count) || 0 : 0;

  return (
    <Modal
      open
      onClose={onClose}
      size={viewing ? 'xl' : 'lg'}
      title="Change impact"
      description="Which indexed files and tests depend on a file, from the imports resolved inside this repository."
      initialFocusRef={inputRef}
    >
      {viewing && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button ref={backRef} variant="secondary" size="sm" icon={ArrowLeft} onClick={() => setViewing(null)}>Back to impact</Button>
            <p className="min-w-0 flex-1 text-[12.5px] text-ink-2">{viewing.caption}</p>
            <Button variant="ghost" size="sm" icon={PanelRight} onClick={openInPanel}>Open in side panel</Button>
          </div>
          <div className="h-[min(62dvh,640px)]">
            <FileViewer
              repoName={repo.repo_name}
              path={viewing.path}
              startLine={viewing.start || null}
              endLine={viewing.end || viewing.start || null}
              githubUrl={result?.github_url || repo.github_url}
              commitSha={result?.commit_sha || repo.commit_sha}
            />
          </div>
        </div>
      )}
      <div ref={bodyRef} hidden={Boolean(viewing)}>
        <form onSubmit={submit} className="space-y-2.5">
          <div>
            <label htmlFor="impact-input" className="text-[12.5px] font-medium text-ink-2">File</label>
            <div className="mt-1 flex gap-2">
              <input id="impact-input" ref={inputRef} value={path} autoComplete="off" spellCheck={false} onChange={(event) => setPath(event.target.value)} placeholder={example} className="h-10 min-w-0 flex-1 rounded-xl border border-line bg-surface px-3 font-mono text-[13px] text-ink outline-none focus:border-accent" />
              <Button type="submit" variant="primary" loading={state.loading} disabled={!path.trim()}>Analyze</Button>
            </div>
          </div>
          <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
            <div className="min-w-[12rem] flex-1">
              <label htmlFor="impact-symbol" className="text-[12.5px] font-medium text-ink-2">Symbol <span className="font-normal text-ink-3">(optional)</span></label>
              <input id="impact-symbol" value={symbol} autoComplete="off" spellCheck={false} onChange={(event) => setSymbol(event.target.value)} placeholder="Function or class name, to find the tests that use it" className="mt-1 h-9 w-full rounded-xl border border-line bg-surface px-3 font-mono text-[12.5px] text-ink outline-none placeholder:font-sans focus:border-accent" />
            </div>
            <label className="flex h-9 items-center gap-2 text-[13px] text-ink-2">
              <input type="checkbox" checked={indirect} onChange={(event) => setDepth(event.target.checked)} className="h-4 w-4 accent-[var(--color-accent)]" />
              Include indirect (up to {MAX_IMPACT_DEPTH} levels)
            </label>
          </div>
        </form>

        {!result && !state.error && !state.loading && (
          <div className="mt-3 space-y-2 text-[12.5px] text-ink-3">
            <p>Enter a path from the repository root, or part of one: a file name, or a path without its extension.</p>
            {suggestions.files.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span>Most imported:</span>
                {suggestions.files.map((file) => (
                  <button key={file.file_path} type="button" onClick={() => choose(file.file_path)} className="rounded-full border border-line bg-surface-2 px-2 py-0.5 font-mono text-[11.5px] text-ink-2 hover:border-line-strong hover:text-ink" title={`Imported by ${file.dependent_count} files`}>
                    {file.file_path}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {graph?.rebuilding && (
          <p className="mt-3 flex gap-2 rounded-xl bg-warning-soft px-3 py-2 text-[12.5px] text-ink-2" role="status">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
            <span>The import graph is being rebuilt from the index. Until it finishes, package imports (and the tests that use them) may be missing. Analyze again in a minute.</span>
          </p>
        )}
        {graph?.rebuilt && !graph?.rebuilding && (
          <p className="mt-3 text-[12px] text-ink-3" role="status">The import graph was rebuilt from the indexed files to include package imports.</p>
        )}
        {state.error && <p className="mt-3 text-[13px] text-danger" role="alert">{state.error}</p>}

        {result?.ambiguous && (
          <div className="mt-5" aria-live="polite">
            <p className="text-sm font-medium text-ink">{result.summary}</p>
            <ul className="mt-2 divide-y divide-line rounded-xl border border-line" aria-label={`Files matching ${result.query}`}>
              {result.candidates.map((candidate) => (
                <li key={candidate.file_path}>
                  <button type="button" onClick={() => choose(candidate.file_path)} className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-surface-2">
                    <span className="truncate font-mono text-[12.5px] text-ink">{candidate.file_path}</span>
                    <span className="shrink-0 text-[12px] text-ink-3">imported by {formatCount(candidate.dependent_count)}</span>
                  </button>
                </li>
              ))}
            </ul>
            {(result.candidate_count > result.candidates.length || result.candidates_complete === false) && (
              <p className="mt-2 text-[12px] text-ink-3">
                Showing {formatCount(result.candidates.length)} of {result.candidates_complete === false ? 'at least ' : ''}{formatCount(result.candidate_count)}. Type more of the path to narrow it down.
              </p>
            )}
          </div>
        )}

        {result?.target_file && (
          <div className="mt-5 space-y-5" aria-live="polite">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <button
                  type="button"
                  data-impact-row="target"
                  onClick={() => open({
                    key: 'target', path: result.target_file, start: result.symbol?.defined_at || null,
                    caption: result.symbol?.defined_at ? `${requestedSymbol} is defined on line ${result.symbol.defined_at}.` : result.target_file,
                  })}
                  className="min-w-0 truncate rounded-md font-mono text-[13px] font-semibold text-ink hover:underline"
                >
                  {result.target_file}
                </button>
                <Button variant="ghost" size="xs" icon={copy.status === 'copied' ? Check : Copy} onClick={copyReport}>
                  {copy.status === 'copied' ? 'Copied' : 'Copy as Markdown'}
                </Button>
              </div>
              <p className="text-sm text-ink">{result.summary}</p>
              {copy.status === 'manual' && (
                <textarea readOnly value={copy.text} aria-label="Impact report as Markdown" rows={6} onFocus={(event) => event.target.select()} className="w-full rounded-xl border border-line bg-surface-2 p-2 font-mono text-[11.5px] text-ink-2" />
              )}
              <p className="sr-only" aria-live="polite">{copy.status === 'copied' ? 'Report copied as Markdown.' : ''}</p>
            </div>

            <section aria-label="Imported by">
              <SectionTitle>
                Imported by ({result.depth > 1 ? `${formatCount(totalDependents)} files, up to ${result.depth} levels` : formatCount(totalDependents)})
              </SectionTitle>
              {groups.length ? groups.map((group) => (
                <DependentGroup key={group.depth} group={group} showDepth={result.depth > 1} onOpen={open} />
              )) : <p className="px-2 text-[13px] text-ink-3">No indexed file imports it.</p>}
              {result.depth === 1 && result.indirect_beyond_depth > 0 && (
                <button type="button" onClick={() => setDepth(true)} className="mt-1 px-2 text-[12.5px] font-medium text-accent-strong hover:underline">
                  Include {formatCount(result.indirect_beyond_depth)} indirect {result.indirect_beyond_depth === 1 ? 'dependent' : 'dependents'}
                </button>
              )}
            </section>

            <section aria-label="Affected tests">
              <SectionTitle aside={<Badge tone="neutral">Static analysis</Badge>}>
                Affected tests ({formatCount(tests?.count || 0)}{moreTests ? ` direct, ${formatCount(moreTests)} indirect` : ''})
              </SectionTitle>
              <p className="mb-1.5 px-2 text-[12px] leading-relaxed text-ink-3">
                Test files that import this file{result.depth > 1 ? ', directly or through other files' : ''}. Found from imports and names, not by running tests, so this is not coverage.
              </p>
              {requestedSymbol && result.symbol && tests?.count > 0 && (
                <div className="mb-2">
                  <p className="px-2 text-[12.5px] font-medium text-ink-2">
                    Test functions for <span className="font-mono">{requestedSymbol}</span>
                    {result.symbol.mentions === 0 && <span className="font-normal text-warning"> · not found in {result.target_file}</span>}
                  </p>
                  {testFunctions.length ? testFunctions.map((file) => (
                    <div key={file.filePath} className="mt-1">
                      <p className="truncate px-2 font-mono text-[11.5px] text-ink-3">{file.filePath}</p>
                      <ul>
                        {file.items.map((item) => (
                          <ImpactRow
                            key={`${item.name}:${item.line}`}
                            rowKey={`fn:${file.filePath}:${item.line}`}
                            path={item.name}
                            line={item.line}
                            detail={testMatchLabel(item, requestedSymbol)}
                            badge={<Badge tone={item.kind === 'reference' ? 'accent' : 'neutral'} className="mt-0.5">{item.kind === 'reference' ? 'uses it' : 'name'}</Badge>}
                            onOpen={() => open({
                              key: `fn:${file.filePath}:${item.line}`, path: file.filePath, start: item.line,
                              end: item.reference_line && item.reference_line - item.line <= 40 ? item.reference_line : item.line,
                              caption: `${item.name}, line ${item.line}: ${testMatchLabel(item, requestedSymbol)}.`,
                            })}
                          />
                        ))}
                      </ul>
                    </div>
                  )) : <p className="px-2 text-[12.5px] text-ink-3">No test in these files uses {requestedSymbol} or is named after it.</p>}
                </div>
              )}
              {requestedSymbol && result.symbol && tests?.files?.length > 0 && (
                <p className="mt-3 px-2 text-[12.5px] font-medium text-ink-2">Test files <span className="font-normal text-ink-3">({formatCount(tests.count)})</span></p>
              )}
              {tests?.files?.length ? (
                <ul>
                  {tests.files.map((file) => (
                    <ImpactRow
                      key={file.file_path}
                      rowKey={`test:${file.file_path}`}
                      path={file.file_path}
                      line={file.line_number}
                      detail={file.depth > 1 ? `${depthLabel(file.depth).toLowerCase()}, via ${file.via}` : 'imports it directly'}
                      onOpen={() => open({
                        key: `test:${file.file_path}`, path: file.file_path, start: file.line_number,
                        caption: `${file.file_path} imports ${file.via} on line ${file.line_number}.`,
                      })}
                    />
                  ))}
                </ul>
              ) : (
                <p className="px-2 text-[13px] text-ink-3">{moreTests ? 'No test imports it directly.' : 'No test file imports it.'}</p>
              )}
              {tests?.truncated && <p className="px-2 pt-1 text-[12px] text-ink-3">Showing {formatCount(tests.files.length)} of {formatCount(tests.count)}.</p>}
              {moreTests > 0 && (
                <button type="button" onClick={() => setDepth(true)} className="mt-1 px-2 text-left text-[12.5px] font-medium text-accent-strong hover:underline">
                  Include the {formatCount(moreTests)} test {moreTests === 1 ? 'file' : 'files'} that import it through other files{requestedSymbol ? ` and search them for ${requestedSymbol}` : ''}
                </button>
              )}
            </section>

            {result.imports?.length > 0 && (
              <section aria-label="Imports">
                <SectionTitle>Imports ({result.imports.length})</SectionTitle>
                <ul>
                  {result.imports.map((item) => (
                    <ImpactRow
                      key={item.file_path}
                      rowKey={`import:${item.file_path}`}
                      path={item.file_path}
                      detail={`imported on line ${item.line_number}`}
                      onOpen={() => open({ key: `import:${item.file_path}`, path: item.file_path, caption: `${result.target_file} imports this file on line ${item.line_number}.` })}
                    />
                  ))}
                </ul>
              </section>
            )}
            <p className="text-[12px] leading-relaxed text-ink-3">{result.method}</p>
          </div>
        )}
      </div>
    </Modal>
  );
};

const PathList = ({ paths, renderItem, label }) => {
  const [filter, setFilter] = useState('');
  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? paths.filter((item) => (typeof item === 'string' ? item : item.path).toLowerCase().includes(needle)) : paths;
  }, [paths, filter]);
  return (
    <div className="border-t border-line">
      {paths.length > 12 && (
        <label className="relative block border-b border-line">
          <span className="sr-only">Filter {label}</span>
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" aria-hidden="true" />
          <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter paths…" className="h-9 w-full bg-transparent pl-9 pr-3 font-mono text-[12px] text-ink outline-none placeholder:font-sans placeholder:text-ink-3" />
        </label>
      )}
      <ul className="scroll-thin max-h-72 overflow-y-auto px-3.5 py-2 font-mono text-[11.5px] leading-6 text-ink-3">
        {visible.map(renderItem)}
        {!visible.length && <li className="font-sans text-[12.5px]">No paths match “{filter}”.</li>}
      </ul>
    </div>
  );
};

const CoverageDialog = ({ repo, onClose }) => {
  const [state, setState] = useState({ loading: true, data: null, error: '' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setState((current) => ({ ...current, loading: true, error: '' }));
    getRepositoryCoverage(repo.repo_name)
      .then((response) => active && setState({ loading: false, data: response.data, error: '' }))
      .catch((error) => active && setState({ loading: false, data: null, error: errorMessage(error, 'Coverage is unavailable.') }));
    return () => { active = false; };
  }, [repo.repo_name, attempt]);
  const data = state.data;
  const reasons = Object.entries(data?.excluded_reasons || {}).sort((a, b) => b[1] - a[1]);
  const selection = data?.selection || {};
  const omittedByCategory = Object.entries(data?.budget_omitted_by_category || {}).filter(([, count]) => count > 0).sort((a, b) => b[1] - a[1]);
  const omittedSample = Array.isArray(data?.budget_omitted_sample) ? data.budget_omitted_sample : [];
  const omittedTotal = omittedByCategory.reduce((sum, [, count]) => sum + count, 0) || selection.budget_omitted_files || 0;
  const seen = Number(data?.total_seen_files) || 0;
  const indexedShare = seen ? Math.round(((Number(data?.indexed_files) || 0) / seen) * 1000) / 10 : 0;
  return (
    <Modal open onClose={onClose} size="lg" title="Coverage report" description="What was indexed, what was skipped, and why.">
      {state.loading ? (
        <div className="space-y-4" aria-busy="true">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">{Array.from({ length: 4 }, (_, index) => <div key={index} className="skeleton h-14" />)}</div>
          {Array.from({ length: 5 }, (_, index) => <div key={index} className="skeleton h-5" style={{ width: `${70 - index * 8}%` }} />)}
          <span className="sr-only" role="status">Loading coverage</span>
        </div>
      ) : state.error ? (
        <div className="rounded-xl border border-danger/30 bg-danger-soft p-4 text-[13px] text-danger" role="alert">
          <p>{state.error}</p>
          <Button className="mt-3" size="sm" variant="secondary" icon={RefreshCw} onClick={() => setAttempt((value) => value + 1)}>Try again</Button>
        </div>
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[['Files seen', data.total_seen_files], ['Indexed', data.indexed_files], ['Skipped', data.excluded_files], ['Skipped size', null]].map(([label, value]) => (
              <div key={label} className="rounded-xl border border-line bg-surface-2/60 px-3 py-2.5">
                <p className="text-[11px] text-ink-3">{label}</p>
                <p className="mt-0.5 font-mono text-[14px] font-semibold text-ink">{value === null ? formatBytes(data.excluded_bytes) : formatCount(value)}</p>
              </div>
            ))}
          </div>
          {seen > 0 && (
            <div>
              <div className="mb-1.5 flex items-baseline justify-between text-[12.5px]">
                <span className="text-ink-2">{indexedShare}% of the files in the snapshot are searchable</span>
                <span className="font-mono text-ink-3">{formatCount(data.indexed_files)} / {formatCount(seen)}</span>
              </div>
              <ProgressBar value={indexedShare} tone="success" label="Share of files indexed" />
            </div>
          )}
          {selection.budget_omitted_files > 0 && (
            <p className="rounded-xl bg-accent-soft px-3.5 py-2.5 text-[12.5px] leading-relaxed text-ink-2">
              Importance budget: the {formatCount(selection.selected_files)} most important of {formatCount(selection.candidate_files)} eligible files were indexed
              {selection.quota_limited ? ', limited by your plan’s indexed-source quota' : ''}. Source, schema and configuration rank first; tests, fixtures, generated code and data rank last.
            </p>
          )}
          <div>
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-ink-3">Skipped by reason</p>
            {reasons.length ? (
              <ul className="divide-y divide-line rounded-xl border border-line">
                {reasons.map(([reason, count]) => (
                  <li key={reason} className="flex items-start justify-between gap-3 px-3.5 py-2.5 text-[13px]">
                    <span className="min-w-0">
                      <span className="block text-ink">{REASON_LABELS[reason] || reason.replaceAll('_', ' ')}</span>
                      {REASON_DETAILS[reason] && <span className="mt-0.5 block text-[12px] leading-snug text-ink-3">{REASON_DETAILS[reason]}</span>}
                    </span>
                    <Badge tone="neutral" className="mt-0.5">{formatCount(count)}</Badge>
                  </li>
                ))}
              </ul>
            ) : <p className="text-[13px] text-ink-3">Nothing was skipped.</p>}
          </div>
          {omittedByCategory.length > 0 && (
            <div>
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-ink-3">Left out by the importance budget</p>
              <ul className="space-y-1.5">
                {omittedByCategory.map(([category, count]) => (
                  <li key={category} className="flex items-center justify-between rounded-lg px-1 text-[13px]">
                    <span className="text-ink-2">{category.replaceAll('_', ' ')}</span>
                    <Badge tone="neutral">{formatCount(count)}</Badge>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {omittedSample.length > 0 && (
            <details className="rounded-xl border border-line">
              <summary className="cursor-pointer px-3.5 py-2.5 text-[13px] font-medium text-ink-2">Budget-omitted files ({shownLabel(omittedSample.length, omittedTotal)})</summary>
              <PathList
                label="budget-omitted files"
                paths={omittedSample}
                renderItem={(item) => (
                  <li key={item.path} className="flex items-center justify-between gap-3">
                    <span className="truncate" title={item.path}>{item.path}</span>
                    {item.category && <span className="shrink-0 font-sans text-[11px]">{String(item.category).replaceAll('_', ' ')}</span>}
                  </li>
                )}
              />
            </details>
          )}
          {data.excluded_paths?.length > 0 && (
            <details className="rounded-xl border border-line">
              <summary className="cursor-pointer px-3.5 py-2.5 text-[13px] font-medium text-ink-2">Skipped paths ({shownLabel(data.excluded_paths_shown ?? data.excluded_paths.length, data.excluded_paths_total)})</summary>
              <PathList label="skipped paths" paths={data.excluded_paths} renderItem={(path) => <li key={path} className="truncate" title={path}>{path}</li>} />
            </details>
          )}
          <p className="text-[12px] leading-relaxed text-ink-3">Answers only cite indexed files. If a file you need was skipped, ask about the code that uses it, or open it on GitHub.</p>
        </div>
      )}
    </Modal>
  );
};

const RepoDialogs = ({ dialog, onClose }) => {
  if (!dialog) return null;
  const props = { repo: dialog.repo, onClose };
  if (dialog.type === 'rename') return <RenameDialog {...props} />;
  if (dialog.type === 'delete') return <DeleteDialog {...props} />;
  if (dialog.type === 'reindex') return <ReindexDialog {...props} />;
  if (dialog.type === 'stop') return <StopDialog {...props} />;
  if (dialog.type === 'impact') return <ImpactDialog {...props} />;
  if (dialog.type === 'coverage') return <CoverageDialog {...props} />;
  return null;
};

export default RepoDialogs;
