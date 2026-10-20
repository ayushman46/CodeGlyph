import { Check, DoorOpen, Layers, MessageSquareText, Square, TriangleAlert } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import {
  KEYWORD_STEP, STEPS, estimateRemainingMs, indexingActivity, phaseFraction, phaseIndex, remainingLabel, trackProgress,
} from '../../lib/phase';
import LoadingState from '../ai/LoadingState';
import Button from '../ui/Button';

export { STEPS, overallProgress, phaseIndex, phaseLabel } from '../../lib/phase';

const VISIBLE_STEPS = STEPS.slice(0, 6);

/**
 * Time left for the step that is running now, measured from the progress
 * this page has observed (semantic %, or "N of M" worker messages). Null
 * until the pace is measurable, so nothing is ever guessed.
 */
export const useIndexingEta = (repo) => {
  const [samples, setSamples] = useState([]);
  const index = phaseIndex(repo);
  const fraction = !repo?.is_indexing ? null : index >= 5 ? (Number(repo.semantic_progress) || 0) / 100 : index > 0 ? phaseFraction(repo) : null;
  const key = `${repo?.id}:${index}`;
  useEffect(() => {
    setSamples((previous) => trackProgress(previous, { key, fraction, at: Date.now() }));
    // Every poll delivers a new repository object; sampling on it lets a
    // stalled step slow the estimate down.
  }, [repo, key, fraction]);
  return repo?.is_indexing ? estimateRemainingMs(samples) : null;
};

/**
 * The live lines under the Loading State label: exact counts, what already
 * works (with a measured time left), budget notes and warnings. Not a live
 * region: counts change on every poll and are read on demand.
 */
const IndexingDetails = ({ activity, brief }) => {
  const counts = [activity.progress, ...activity.facts].filter(Boolean).join(' · ');
  const usable = [brief && activity.progress, activity.usable, activity.remaining && `${activity.remaining} in this step`].filter(Boolean).join(' · ');
  return (
    <span className="flex flex-col gap-1">
      {!brief && counts && <span data-slot="indexing-counts" className="block">{counts}</span>}
      {usable && <span data-slot="indexing-usable" className="block">{usable}</span>}
      {!brief && activity.notes.map((note) => (
        <span key={note} className="flex items-start gap-1">
          <Layers className="mt-px size-3 shrink-0 text-ink-3" aria-hidden="true" />
          <span className="min-w-0">{note}</span>
        </span>
      ))}
      {activity.warnings.map((warning) => (
        <span key={warning} data-slot="indexing-warning" className="flex items-start gap-1 text-ink">
          <TriangleAlert className="mt-px size-3 shrink-0 text-warning" strokeWidth={1.75} aria-hidden="true" />
          <span className="min-w-0"><span className="sr-only">Warning: </span>{warning}</span>
        </span>
      ))}
    </span>
  );
};

/**
 * Loading State for an indexing repository: the step in progress as its
 * announced label ("Chunking 6,398 files", "Building semantic index"), the
 * time since the worker started the job (from the API, so it survives
 * reloads) and the live details from the status poll, which carry the
 * moving counts ("65% of files read") without announcing every poll.
 * `brief` keeps only the step's progress, what already works and the
 * warnings (for a one-line strip); `messageShown` says the worker's message
 * is already on screen.
 */
export const IndexingLoadingState = ({ repo, eta = null, brief = false, messageShown = false, className, ...props }) => {
  const activity = indexingActivity(repo, { remaining: remainingLabel(eta), messageShown });
  return (
    <LoadingState
      label={activity.label}
      pattern={activity.pattern}
      startedAt={activity.startedAt}
      details={<IndexingDetails activity={activity} brief={brief} />}
      className={className}
      data-phase={repo?.status}
      {...props}
    />
  );
};

const StepIcon = ({ state }) => {
  if (state === 'done') {
    return <span className="flex h-5 w-5 items-center justify-center rounded-full bg-success text-white dark:text-canvas"><Check className="h-3 w-3" strokeWidth={3} aria-hidden="true" /></span>;
  }
  // Still: the Loading State above is the one moving indicator. Drawn as a
  // border so it survives forced colors.
  if (state === 'active') return <span className="flex h-5 w-5 items-center justify-center"><span className="h-2.5 w-2.5 rounded-full border-[5px] border-accent ring-4 ring-accent/20" aria-hidden="true" /></span>;
  return <span className="flex h-5 w-5 items-center justify-center"><span className="h-2 w-2 rounded-full border-2 border-line-strong" aria-hidden="true" /></span>;
};

const IngestionProgress = ({ repo, onStop, stopping = false }) => {
  const titleId = useId();
  const index = phaseIndex(repo);
  const eta = useIndexingEta(repo);
  const stepNumber = Math.min(index + 1, VISIBLE_STEPS.length);

  return (
    <section className="w-full rounded-2xl border border-line bg-surface p-5 shadow-[var(--shadow-soft)] sm:p-6" aria-labelledby={titleId}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">{repo.refreshing ? 'Refreshing index' : 'Indexing'} · step {stepNumber} of {VISIBLE_STEPS.length}</p>
          <h2 id={titleId} className="mt-1.5 truncate text-lg font-semibold tracking-tight text-ink">{repo.full_name || repo.repo_name}</h2>
          {/* The worker's own words; the Loading State below says what is happening when it has none. */}
          {repo.message && <p className="mt-1 text-sm leading-relaxed text-ink-2">{repo.message}</p>}
        </div>
        {onStop && (
          <Button size="sm" variant="secondary" icon={Square} loading={stopping} disabled={stopping} onClick={onStop} aria-label={`Stop indexing ${repo.repo_name}`}>
            {stopping ? 'Stopping…' : 'Stop'}
          </Button>
        )}
      </div>

      <IndexingLoadingState repo={repo} eta={eta} messageShown={Boolean(repo.message)} className="mt-5" />

      <div className="mt-6 grid gap-6 md:grid-cols-[minmax(0,1fr)_15rem]">
        <ol className="relative space-y-0.5" aria-label="Indexing steps">
          {VISIBLE_STEPS.map((step, stepIndex) => {
            const state = stepIndex < index ? 'done' : stepIndex === index ? 'active' : 'pending';
            const unlocks = stepIndex === KEYWORD_STEP && state !== 'done';
            return (
              <li key={step.id} className={`relative flex items-start gap-3 rounded-xl px-2.5 py-1.5 ${state === 'active' ? 'bg-accent-soft' : ''}`}>
                {stepIndex < VISIBLE_STEPS.length - 1 && (
                  <span className={`absolute left-[19px] top-7 h-[calc(100%-1.25rem)] w-px ${state === 'done' ? 'bg-success/50' : 'bg-line'}`} aria-hidden="true" />
                )}
                <span className="relative mt-px shrink-0 bg-inherit"><StepIcon state={state} /></span>
                <span className="min-w-0 flex-1">
                  <span className={`flex flex-wrap items-center gap-x-2 text-[13px] ${state === 'active' ? 'font-semibold text-ink' : state === 'done' ? 'text-ink-2' : 'text-ink-3'}`}>
                    {step.label}
                    {step.id === 'searchable' && state === 'active' && <span className="font-mono text-[12px] font-normal text-ink-2">{repo.semantic_progress}%</span>}
                    {unlocks && <span className="rounded-full bg-surface-2 px-1.5 py-px text-[10.5px] font-medium text-ink-2">Questions unlock after this</span>}
                    <span className="sr-only">{state === 'done' ? '(done)' : state === 'active' ? '(in progress)' : '(waiting)'}</span>
                  </span>
                  {state === 'active' && <span className="mt-0.5 block text-[12px] leading-snug text-ink-2">{step.detail}</span>}
                </span>
              </li>
            );
          })}
        </ol>

        <aside className="rounded-xl bg-surface-2/70 p-4 text-[12.5px] leading-relaxed text-ink-2" aria-label="While you wait">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-ink-3">While you wait</p>
          <ul className="mt-2.5 space-y-2.5">
            <li className="flex gap-2"><MessageSquareText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" /><span>Ask questions as soon as keyword search is ready. This page switches by itself.</span></li>
            <li className="flex gap-2"><DoorOpen className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" /><span>Indexing keeps running if you leave this page or close the tab.</span></li>
          </ul>
        </aside>
      </div>
    </section>
  );
};

export default IngestionProgress;
