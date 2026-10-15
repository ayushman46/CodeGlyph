import { AlertTriangle, FileCode2, RefreshCw, Search, Square } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { errorMessage, getRepositoryCoverage } from '../../api/client';
import { formatBytes, formatCount, shownLabel } from '../../lib/format';
import useStore from '../../store/useStore';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import ProgressBar from '../ui/ProgressBar';
import Spinner from '../ui/Spinner';

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

const ImpactDialog = ({ repo, onClose }) => {
  const analyzeImpact = useStore((state) => state.analyzeImpact);
  const clearImpactAnalysis = useStore((state) => state.clearImpactAnalysis);
  const impactAnalysis = useStore((state) => state.impactAnalysis);
  const impactAnalysisLoading = useStore((state) => state.impactAnalysisLoading);
  const impactAnalysisError = useStore((state) => state.impactAnalysisError);
  const openFile = useStore((state) => state.openFile);
  const [path, setPath] = useState('');
  useEffect(() => () => clearImpactAnalysis(), [clearImpactAnalysis]);
  const submit = (event) => {
    event.preventDefault();
    if (path.trim()) void analyzeImpact(repo, path.trim());
  };
  const FileLink = ({ value }) => (
    <button type="button" onClick={() => { openFile({ path: value }); onClose(); }} className="flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left font-mono text-[12.5px] text-ink-2 hover:bg-surface-2 hover:text-ink">
      <FileCode2 className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" /> <span className="truncate">{value}</span>
    </button>
  );
  return (
    <Modal open onClose={onClose} size="md" title="Change impact" description="Which indexed files import a file, and what it imports. Only imports resolved inside this repository are shown.">
      <form onSubmit={submit} className="flex gap-2">
        <label htmlFor="impact-input" className="sr-only">File path</label>
        <input id="impact-input" value={path} autoFocus autoComplete="off" spellCheck={false} onChange={(event) => setPath(event.target.value)} placeholder="src/lib/auth.ts" className="h-10 min-w-0 flex-1 rounded-xl border border-line bg-surface px-3 font-mono text-[13px] text-ink outline-none focus:border-accent" />
        <Button type="submit" variant="primary" loading={impactAnalysisLoading} disabled={!path.trim()}>Analyze</Button>
      </form>
      {!impactAnalysis && !impactAnalysisError && !impactAnalysisLoading && (
        <p className="mt-3 text-[12.5px] text-ink-3">Enter a path relative to the repository root, as shown in the folder map or a citation.</p>
      )}
      {impactAnalysisError && <p className="mt-3 text-[13px] text-danger" role="alert">{impactAnalysisError}</p>}
      {impactAnalysis && (
        <div className="mt-5 space-y-4" aria-live="polite">
          <p className="text-sm font-medium text-ink">{impactAnalysis.summary}</p>
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-ink-3">
              Imported by ({impactAnalysis.truncated || (impactAnalysis.dependent_count ?? 0) > impactAnalysis.dependent_files.length
                ? `${formatCount(impactAnalysis.dependent_count ?? impactAnalysis.dependent_files.length)} files, showing ${formatCount(impactAnalysis.dependent_files.length)}`
                : impactAnalysis.dependent_files.length})
            </p>
            {impactAnalysis.dependent_files.length ? impactAnalysis.dependent_files.map((value) => <FileLink key={value} value={value} />) : <p className="text-[13px] text-ink-3">No resolved dependents.</p>}
          </div>
          {impactAnalysis.imported_files?.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-ink-3">Imports ({impactAnalysis.imported_files.length})</p>
              {impactAnalysis.imported_files.map((value) => <FileLink key={value} value={value} />)}
            </div>
          )}
        </div>
      )}
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
