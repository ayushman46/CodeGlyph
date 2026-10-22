import {
  AlertTriangle, CircleCheck, CircleSlash, CircleX, ExternalLink, Info, RefreshCw, Wrench,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { getPullRequestStatus } from '../../api/client';
import {
  approximatePaths, ciCheckOutcome, ciCountsText, ciError, ciPollDelay, ciStateLabel, ciStateTone, incompleteness,
  mergeStateMessage, qualityNote, qualitySummary, safeGithubUrl, safeHttpsUrl,
} from '../../lib/proposal';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Spinner from '../ui/Spinner';

const AUTOFIX_LABELS = {
  final_newline: 'final newline',
  trailing_whitespace: 'trailing whitespace',
  indentation: 'indentation',
  import_extension: 'import extension',
};

const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

/** "source/a.ts:30:1" for a finding (or a review issue). */
const where = ({ file_path: path, line, column }) => (
  path ? `${path}${line ? `:${line}${column ? `:${column}` : ''}` : ''}` : ''
);

/** Findings as `file:line message` lines. */
const FindingList = ({ findings }) => (findings.length ? (
  <ul className="mt-1 space-y-0.5">
    {findings.map((finding, index) => (
      // eslint-disable-next-line react/no-array-index-key
      <li key={index} className="break-words">
        {where(finding) && <span className="font-mono text-[12px] text-ink">{where(finding)}</span>}
        {where(finding) && finding.message ? ' ' : null}
        {finding.message}
      </li>
    ))}
  </ul>
) : null);

const CheckList = ({ checks }) => (
  <ul className="mt-1.5 space-y-2">
    {checks.map((check) => (
      <li key={check.id} data-check={check.id}>
        <p className="m-0">
          <span className="font-semibold text-ink">{check.label}</span>
          {check.detail ? <span>: {check.detail}</span> : null}
        </p>
        <FindingList findings={check.files} />
      </li>
    ))}
  </ul>
);

/** "Self-review: …" / "Self-review skipped: …" (the server may already say "Self-review skipped:"). */
const reviewLine = (summary) => {
  const text = summary.reviewSummary;
  if (summary.reviewed) return text ? (/^self-review\b/i.test(text) ? text : `Self-review: ${text}`) : 'Self-review: no issues found.';
  if (!text) return 'Self-review skipped.';
  return /^self-review skipped\b/i.test(text) ? text : `Self-review skipped: ${text}`;
};

/**
 * Warnings the server attached to a proposal: its quality gates (failed
 * checks in a danger box, warnings in a warning box, the self-review line,
 * automatic fixes), files it planned to change but did not
 * (`unchanged_files` / `incomplete`) and files whose content was
 * reconstructed from the index (`approximate_files`). A proposal without
 * `quality` (an older one) renders exactly as before.
 */
const ProposalNotices = ({ proposal, refreshedPaths = new Set(), className = '' }) => {
  const { unchanged, incomplete } = incompleteness(proposal);
  const approximate = [...approximatePaths(proposal)].filter((path) => !refreshedPaths.has(path));
  const quality = qualitySummary(proposal);
  const hasQuality = Boolean(quality.status);
  if (!incomplete && !approximate.length && !hasQuality) return null;
  // The self-review has its own line below; its unfixed issues join the warnings.
  const warnings = quality.warnings.filter((check) => check.id !== 'review');
  const unfixed = quality.reviewIssues.filter((issue) => !issue.fixed && issue.verified);
  const suggestions = quality.reviewIssues.filter((issue) => !issue.fixed && !issue.verified);
  const fixed = quality.reviewIssues.filter((issue) => issue.fixed);
  const reviewOk = quality.reviewed || quality.reviewSkippedOnPurpose;
  const passed = quality.passed.filter((check) => check.id !== 'review').map((check) => check.label.toLowerCase());
  return (
    <div className={`space-y-2 ${className}`} data-slot="proposal-notices">
      {quality.failing.length > 0 && (
        <div className="rounded-xl border border-danger/30 bg-danger-soft px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2" role="note" data-slot="quality-fail">
          <p className="m-0 flex items-center gap-2 font-semibold text-danger">
            <CircleX className="h-4 w-4 shrink-0" aria-hidden="true" />
            Needs attention: {qualityNote(quality)}
          </p>
          <CheckList checks={quality.failing} />
          <p className="m-0 mt-2 text-ink-3">Fix these in the review editor or ask again. Pushing anyway needs your confirmation.</p>
        </div>
      )}
      {(warnings.length > 0 || unfixed.length > 0) && (
        <div className="rounded-xl border border-warning/40 bg-warning-soft px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2" role="note" data-slot="quality-warn">
          <p className="m-0 flex items-center gap-2 font-semibold text-ink">
            <AlertTriangle className="h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
            {plural(warnings.length + (unfixed.length ? 1 : 0), 'warning')} to check before pushing
          </p>
          {warnings.length > 0 && <CheckList checks={warnings} />}
          {unfixed.length > 0 && (
            <div className="mt-2">
              <p className="m-0 font-semibold text-ink">The self-review found issues it could not fix automatically:</p>
              <ul className="mt-1 space-y-0.5">
                {unfixed.map((issue, index) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <li key={index} className="break-words">
                    <span className="text-ink-3">{issue.severity}</span>
                    {where(issue) ? <> · <span className="font-mono text-[12px] text-ink">{where(issue)}</span></> : null}
                    {' '}{issue.problem}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      {hasQuality && (
        <div className="space-y-1 px-0.5 text-[12px] leading-relaxed text-ink-3" data-slot="quality-notes">
          {passed.length > 0 && (
            <p className="m-0 flex items-start gap-1.5" data-slot="quality-passed">
              <CircleCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success" aria-hidden="true" />
              <span>Passed: {passed.join(', ')}.</span>
            </p>
          )}
          <p className="m-0 flex items-start gap-1.5" data-slot="quality-review">
            {reviewOk
              ? <CircleCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success" aria-hidden="true" />
              : <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />}
            <span>
              {reviewLine(quality)}
              {fixed.length > 0 && ` Fixed ${plural(fixed.length, 'issue')}.`}
            </span>
          </p>
          {fixed.length > 0 && (
            <details className="ms-5">
              <summary className="cursor-pointer select-none">What the self-review fixed</summary>
              <ul className="mt-1 space-y-0.5">
                {fixed.map((issue, index) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <li key={index} className="break-words">
                    {where(issue) && <span className="font-mono text-[11.5px]">{where(issue)} </span>}
                    {issue.problem}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {suggestions.length > 0 && (
            <details className="ms-5" data-slot="quality-suggestions">
              <summary className="cursor-pointer select-none">
                {plural(suggestions.length, 'reviewer suggestion')} not applied (no automatic finding or quoted code backs {suggestions.length === 1 ? 'it' : 'them'})
              </summary>
              <ul className="mt-1 space-y-0.5">
                {suggestions.map((issue, index) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <li key={index} className="break-words">
                    {where(issue) && <span className="font-mono text-[11.5px]">{where(issue)} </span>}
                    {issue.problem}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {quality.autofixed.length > 0 && (
            <p className="m-0 flex items-start gap-1.5" data-slot="quality-autofixed">
              <Wrench className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>
                Fixed automatically:{' '}
                {quality.autofixed.map((fix, index) => (
                  <span key={`${fix.file_path}:${fix.rule}`}>
                    {index > 0 ? ', ' : ''}
                    {AUTOFIX_LABELS[fix.rule] || fix.rule.replace(/_/g, ' ')} in <span className="font-mono text-[11.5px]">{fix.file_path}</span>
                    {fix.lines > 1 ? ` (${fix.lines} lines)` : ''}
                  </span>
                ))}
                .
              </span>
            </p>
          )}
          {quality.skipped.length > 0 && (
            <p className="m-0 flex items-start gap-1.5" data-slot="quality-skipped">
              <CircleSlash className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>
                Not checked:{' '}
                {quality.skipped.map((check, index) => `${index ? '; ' : ''}${check.label}${check.detail ? ` (${check.detail})` : ''}`).join('')}.
              </span>
            </p>
          )}
        </div>
      )}
      {incomplete && (
        <div className="rounded-xl border border-warning/40 bg-warning-soft px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2" role="note">
          <p className="flex items-center gap-2 font-semibold text-ink">
            <AlertTriangle className="h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
            This change may be incomplete
          </p>
          {unchanged.length > 0 ? (
            <>
              <p className="mt-1">These files were part of the plan but were not changed:</p>
              <ul className="mt-1 space-y-0.5">
                {unchanged.map((item) => (
                  <li key={item.file_path} className="break-words">
                    <span className="font-mono text-[12px] text-ink">{item.file_path}</span>
                    {item.reason ? <span> — {item.reason}</span> : null}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="mt-1">Some planned changes could not be written. Review carefully or ask again with a narrower request.</p>
          )}
        </div>
      )}
      {approximate.length > 0 && (
        <div className="rounded-xl border border-info/30 bg-info-soft px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2" role="note">
          <p className="flex items-center gap-2 font-semibold text-ink">
            <Info className="h-4 w-4 shrink-0 text-info" aria-hidden="true" />
            {approximate.length === 1 ? '1 file was' : `${approximate.length} files were`} reconstructed from the index
          </p>
          <p className="mt-1">Refresh from GitHub before pushing so the change is applied to the real file:</p>
          <ul className="mt-1 space-y-0.5">
            {approximate.map((path) => <li key={path} className="break-words font-mono text-[12px] text-ink">{path}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
};

const SUMMARY_STYLE = {
  pass: { box: 'border-success/30 bg-success-soft', icon: CircleCheck, iconClass: 'text-success' },
  warn: { box: 'border-warning/40 bg-warning-soft', icon: AlertTriangle, iconClass: 'text-warning' },
  fail: { box: 'border-danger/30 bg-danger-soft', icon: CircleX, iconClass: 'text-danger' },
};

/**
 * The quality gates in a few lines (the review's PR form): what failed and
 * what to check, with the full list in ProposalNotices beside the diff.
 * Nothing for a proposal without `quality`.
 */
export const QualitySummary = ({ proposal, className = '' }) => {
  const quality = qualitySummary(proposal);
  if (!quality.status) return null;
  const style = SUMMARY_STYLE[quality.status];
  const Icon = style.icon;
  const headline = quality.status === 'pass'
    ? 'All checks passed'
    : quality.status === 'fail' ? `Checks failed: ${qualityNote(quality)}` : `${plural(Math.max(1, quality.warnings.length), 'warning')} to check`;
  const items = [...quality.failing, ...quality.warnings.filter((check) => check.id !== 'review')];
  return (
    <div className={`rounded-xl border px-3 py-2.5 text-[12.5px] leading-relaxed text-ink-2 ${style.box} ${className}`} data-slot="quality-summary" data-status={quality.status}>
      <p className="m-0 flex items-center gap-2 font-semibold text-ink">
        <Icon className={`h-4 w-4 shrink-0 ${style.iconClass}`} aria-hidden="true" />
        {headline}
      </p>
      {items.length > 0 && (
        <ul className="mt-1 space-y-0.5 ps-6">
          {items.map((check) => (
            <li key={check.id} className="break-words">
              <span className={check.status === 'fail' ? 'font-semibold text-danger' : 'font-semibold text-ink'}>{check.label}</span>
              {check.files[0] ? <span>: <span className="font-mono text-[11.5px]">{where(check.files[0])}</span> {check.files[0].message}</span> : check.detail ? <span>: {check.detail}</span> : null}
              {check.files.length > 1 ? <span className="text-ink-3"> (+{check.files.length - 1} more)</span> : null}
            </li>
          ))}
        </ul>
      )}
      <p className="m-0 mt-1 ps-6 text-[12px] text-ink-3">{reviewLine(quality)}</p>
    </div>
  );
};

// ------------------------------------------------------------- CI status

/**
 * CI checks and merge readiness of a pull request (GET /github/pr-status).
 * Loads once when `enabled`; with `poll`, refreshes every 20 s while checks
 * are pending, for at most 10 minutes, and stops on unmount.
 */
export const usePullRequestStatus = ({ repoName, prNumber, enabled = true, poll = false }) => {
  const [status, setStatus] = useState({ data: null, error: null, loading: false });
  const aliveRef = useRef(true);
  const timerRef = useRef(null);
  const controllerRef = useRef(null);
  const startedRef = useRef(Date.now());
  const lastStateRef = useRef(null);
  const loadRef = useRef(null);
  const number = Number(prNumber);
  const ready = enabled && Boolean(repoName) && Number.isInteger(number) && number > 0;

  const load = useCallback(async () => {
    if (!ready) return;
    window.clearTimeout(timerRef.current);
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setStatus((current) => ({ ...current, loading: true }));
    let state = null;
    let retryAfter = null;
    try {
      const response = await getPullRequestStatus(repoName, number, { signal: controller.signal });
      if (!aliveRef.current || controller.signal.aborted) return;
      state = response.data?.state ?? null;
      lastStateRef.current = state;
      setStatus({ data: response.data, error: null, loading: false });
    } catch (error) {
      if (!aliveRef.current || controller.signal.aborted) return;
      const failure = ciError(error);
      // A rate limit keeps polling after Retry-After; other errors stop it.
      if (failure.retryAfter != null) {
        state = lastStateRef.current;
        retryAfter = failure.retryAfter;
      }
      setStatus((current) => ({ ...current, error: failure, loading: false }));
    }
    if (!poll) return;
    const delay = ciPollDelay({ state, startedAt: startedRef.current, retryAfter });
    if (delay != null) timerRef.current = window.setTimeout(() => loadRef.current?.(), delay);
  }, [ready, repoName, number, poll]);
  loadRef.current = load;

  useEffect(() => {
    aliveRef.current = true;
    if (ready) void load();
    return () => {
      aliveRef.current = false;
      window.clearTimeout(timerRef.current);
      controllerRef.current?.abort();
    };
  }, [ready, load]);

  return { ...status, refresh: load };
};

/**
 * The CI panel shown after a push: overall state, counts, merge readiness,
 * one row per check (failures first, linked when the CI gave an https link),
 * and a Refresh button.
 */
export const PullRequestChecks = ({ repoName, prNumber, poll = true, className = '' }) => {
  const { data, error, loading, refresh } = usePullRequestStatus({ repoName, prNumber, poll });
  const checks = Array.isArray(data?.checks) ? data.checks : [];
  const counts = ciCountsText(data?.counts);
  const merge = mergeStateMessage(data?.pr);
  const checksUrl = safeGithubUrl(data?.checks_url);
  return (
    <section aria-label="Pull request checks" data-slot="ci-panel" className={`rounded-2xl border border-line bg-surface p-4 text-left ${className}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <h4 className="m-0 text-[13px] font-semibold text-ink">CI checks</h4>
          {data && <Badge tone={ciStateTone(data.state)} dot data-slot="ci-state">{ciStateLabel(data.state)}</Badge>}
        </div>
        <Button size="xs" variant="ghost" icon={RefreshCw} loading={loading} onClick={() => { void refresh(); }}>Refresh checks</Button>
      </div>
      {error && <p className="m-0 mt-2 text-[12.5px] text-danger" role="status">{error.message}</p>}
      {!data && !error && (
        <p className="m-0 mt-2 flex items-center gap-2 text-[12.5px] text-ink-3"><Spinner className="h-3.5 w-3.5" /> Loading checks from GitHub…</p>
      )}
      {data && (
        <div className="mt-2 space-y-1.5 text-[12.5px] leading-relaxed text-ink-2">
          {data.summary && <p className="m-0" data-slot="ci-summary">{data.summary}</p>}
          {counts && <p className="m-0 text-ink-3" data-slot="ci-counts">{counts}</p>}
          {data.state === 'action_required' && (
            <p className="m-0">A maintainer has to approve the workflows before CI runs on a pull request from a fork.</p>
          )}
          {data.state === 'pending' && poll && <p className="m-0 text-ink-3">Refreshing every 20 seconds while checks run.</p>}
          {merge && <p className="m-0" data-slot="ci-merge">{merge}</p>}
          {checks.length > 0 && (
            <ul className="scroll-thin max-h-56 space-y-1 overflow-y-auto pt-1" aria-label="Checks">
              {checks.map((check, index) => {
                const outcome = ciCheckOutcome(check);
                const url = safeHttpsUrl(check.url);
                const name = String(check.name || 'Check');
                return (
                  // eslint-disable-next-line react/no-array-index-key
                  <li key={`${name}:${index}`} className="flex min-w-0 items-center justify-between gap-2">
                    {url ? (
                      <a href={url} target="_blank" rel="noreferrer noopener" className="min-w-0 truncate text-ink hover:underline" title={name}>{name}</a>
                    ) : (
                      <span className="min-w-0 truncate text-ink" title={name}>{name}</span>
                    )}
                    <Badge tone={outcome.tone}>{outcome.label}</Badge>
                  </li>
                );
              })}
            </ul>
          )}
          {checksUrl && (
            <a href={checksUrl} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 font-medium text-accent hover:underline">
              All checks on GitHub <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
            </a>
          )}
        </div>
      )}
    </section>
  );
};

export default ProposalNotices;
