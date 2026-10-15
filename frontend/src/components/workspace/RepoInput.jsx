import { ArrowRight, CheckCircle2, Github } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { errorMessage } from '../../api/client';
import { parseRepositoryInput } from '../../lib/format';
import useStore from '../../store/useStore';
import Spinner from '../ui/Spinner';

const EXAMPLES = ['calcom/cal.com', 'tiangolo/fastapi', 'shadcn-ui/ui'];

const repoPath = (name) => `/app/r/${encodeURIComponent(name)}`;

/** The workspace repository for owner/repo, ignoring case (GitHub names are case-insensitive). */
const findExisting = (repos, fullName) => {
  const wanted = fullName.toLowerCase();
  return repos.find((repo) => (repo.full_name || '').toLowerCase() === wanted) || null;
};

/**
 * "Add repository" field: validates as you type with the server's rules,
 * opens a repository you already have instead of re-indexing it, and turns
 * server refusals into a next step.
 */
const RepoInput = ({ size = 'lg', showExamples = true, autoFocus = false, initialValue = '' }) => {
  const ingest = useStore((state) => state.ingest);
  const repos = useStore((state) => state.repos);
  const navigate = useNavigate();
  const id = useId();
  const [value, setValue] = useState(initialValue);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [touched, setTouched] = useState(false);
  const inputRef = useRef(null);
  const large = size === 'lg';
  const parsed = parseRepositoryInput(value);
  const existing = parsed.ok ? findExisting(repos, parsed.fullName) : null;

  // A value handed over (e.g. "Index owner/repo" in the command palette) is
  // shown for confirmation, never submitted automatically.
  useEffect(() => {
    if (!initialValue) return;
    setValue(initialValue);
    setError(null);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [initialValue]);

  useEffect(() => {
    if (!value.trim() || touched) return undefined;
    const timer = window.setTimeout(() => setTouched(true), 900);
    return () => window.clearTimeout(timer);
  }, [value, touched]);

  // Validation feedback appears once the user pauses or submits, never on the first keystroke.
  const liveError = touched && !parsed.ok && parsed.reason !== 'empty' ? parsed.error : '';
  const message = error?.message || liveError;
  const describedBy = `${id}-feedback`;

  const submit = async (event, override) => {
    event?.preventDefault();
    const input = (override ?? value).trim();
    if (!input || busy) return;
    const target = parseRepositoryInput(input);
    setTouched(true);
    if (!target.ok) {
      setError({ message: target.error });
      return;
    }
    // Already in the workspace: open it (a plain owner/repo link would only
    // queue a full re-index of the same code).
    const known = findExisting(repos, target.fullName);
    if (known && !target.ref) {
      setValue('');
      setError(null);
      setTouched(false);
      navigate(repoPath(known.repo_name), { state: { notice: `${target.fullName} is already in your workspace. Use Re-index in the ⋯ menu to fetch the latest commit.` } });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const repoName = await ingest(input);
      setValue('');
      setTouched(false);
      if (repoName) navigate(repoPath(repoName));
    } catch (submitError) {
      const status = submitError?.response?.status;
      const indexing = repos.find((repo) => repo.is_indexing);
      setError({
        message: errorMessage(submitError, 'Could not queue this repository for indexing. Check the URL and try again.'),
        // Only one indexing job runs per workspace: point at the running one.
        action: status === 409 && indexing ? { label: `Open ${indexing.repo_name}`, to: repoPath(indexing.repo_name) } : null,
      });
    } finally {
      setBusy(false);
    }
  };

  const hint = parsed.ok && !message
    ? existing && !parsed.ref
      ? { tone: 'info', text: `Already in your workspace. Press Enter to open ${existing.repo_name}.` }
      : { tone: 'ok', text: `Ready to index ${parsed.fullName}${parsed.ref ? (parsed.ref.kind === 'commit' ? ` at commit ${parsed.ref.label}` : ` at ${parsed.ref.label}`) : ''}.` }
    : null;

  return (
    <div>
      <form
        onSubmit={submit}
        noValidate
        className={`flex items-center gap-2 border bg-surface shadow-[var(--shadow-soft)] transition-colors focus-within:border-accent/60 ${message ? 'border-danger/50' : 'border-line'} ${large ? 'rounded-2xl p-1.5' : 'rounded-xl p-1'}`}
      >
        <Github className={`shrink-0 text-ink-3 ${large ? 'ml-2.5 h-5 w-5' : 'ml-2 h-4 w-4'}`} aria-hidden="true" />
        <label htmlFor={`repo-input-${size}`} className="sr-only">GitHub repository URL or owner/repo</label>
        <input
          id={`repo-input-${size}`}
          ref={inputRef}
          value={value}
          autoFocus={autoFocus}
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="go"
          onChange={(event) => { setValue(event.target.value); if (error) setError(null); }}
          onBlur={() => { if (value.trim()) setTouched(true); }}
          onPaste={() => setTouched(true)}
          placeholder={large ? 'owner/repo or GitHub URL' : 'Add repository (owner/repo)'}
          className={`min-w-0 flex-1 bg-transparent text-ink outline-none placeholder:text-ink-3 ${large ? 'h-11 text-[15px]' : 'h-8 text-[13px]'}`}
          readOnly={busy}
          aria-invalid={message ? true : undefined}
          aria-describedby={describedBy}
        />
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className={`flex shrink-0 items-center justify-center gap-1.5 bg-accent font-medium text-accent-ink transition-colors hover:bg-accent-strong disabled:bg-surface-3 disabled:text-ink-3 ${large ? 'h-11 rounded-xl px-4 text-sm' : 'h-8 w-8 rounded-lg'}`}
          aria-label={existing && !parsed.ref ? `Open ${existing.repo_name}` : 'Index repository'}
        >
          {busy ? <Spinner className="h-4 w-4" /> : large ? <>{existing && !parsed.ref ? 'Open' : 'Index'} <ArrowRight className="h-4 w-4" aria-hidden="true" /></> : <ArrowRight className="h-4 w-4" aria-hidden="true" />}
        </button>
      </form>
      <div id={describedBy} aria-live="polite" className={message || (large && hint) || busy ? 'mt-2' : ''}>
        {busy ? (
          <p className="text-[12.5px] text-ink-3">Queuing {parsed.ok ? parsed.fullName : 'repository'} for indexing…</p>
        ) : message ? (
          <p className="text-[12.5px] leading-relaxed text-danger" role="alert">
            {message}
            {error?.action && <> <Link to={error.action.to} className="font-semibold underline underline-offset-2">{error.action.label}</Link></>}
          </p>
        ) : large && hint ? (
          <p className={`flex items-center gap-1.5 text-[12.5px] ${hint.tone === 'ok' ? 'text-success' : 'text-ink-2'}`}>
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" aria-hidden="true" /> {hint.text}
          </p>
        ) : null}
      </div>
      {showExamples && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
          <span>Try an example:</span>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              disabled={busy}
              onClick={(event) => { setValue(example); void submit(event, example); }}
              className="rounded-lg border border-line bg-surface px-2 py-1 font-mono text-[11.5px] text-ink-2 transition-colors hover:border-accent/50 hover:text-ink disabled:opacity-50"
              aria-label={findExisting(repos, example) ? `Open ${example}` : `Index ${example}`}
            >
              {example}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

export default RepoInput;
