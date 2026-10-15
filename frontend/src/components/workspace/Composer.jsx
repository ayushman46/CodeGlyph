import { ArrowUp, ChevronDown, Clock, GitPullRequest, MessageSquareText, SlidersHorizontal, Square } from 'lucide-react';
import { forwardRef, useEffect, useId, useImperativeHandle, useRef, useState } from 'react';
import useMediaQuery from '../../hooks/useMediaQuery';
import useStore from '../../store/useStore';
import Kbd from '../ui/Kbd';
import Segmented from '../ui/Segmented';
import Spinner from '../ui/Spinner';

export const WORKFLOWS = [
  { value: 'general', label: 'General question', short: 'General' },
  { value: 'onboarding', label: 'New engineer onboarding', short: 'Onboarding' },
  { value: 'architecture', label: 'Architecture deep dive', short: 'Architecture' },
  { value: 'security', label: 'Security review', short: 'Security' },
  { value: 'contributor', label: 'Open-source contributor', short: 'Contributor' },
  { value: 'due_diligence', label: 'Technical due diligence', short: 'Due diligence' },
];

const MODELS = [
  { value: 'fast', label: 'Fast · Super 120B', short: 'Fast' },
  { value: 'detailed', label: 'Detailed · Ultra 550B', short: 'Detailed' },
];

// The API rejects longer questions; count down before that happens.
const MAX_QUESTION_LENGTH = 8000;
const WARN_QUESTION_LENGTH = 7000;

const PREFERENCES_KEY = 'ci-composer';

const readPreferences = () => {
  try {
    const value = JSON.parse(window.localStorage.getItem(PREFERENCES_KEY) || '{}');
    return {
      mode: value.mode === 'edit' ? 'edit' : 'ask',
      workflow: WORKFLOWS.some((item) => item.value === value.workflow) ? value.workflow : 'general',
      model: value.model === 'detailed' ? 'detailed' : 'fast',
    };
  } catch {
    return { mode: 'ask', workflow: 'general', model: 'fast' };
  }
};

const shortLabel = (options, value) => options.find((option) => option.value === value)?.short || '';

/**
 * A compact pill that shows a short label while the native select (stretched
 * invisibly over it) keeps the full option names, keyboard support and the
 * platform picker on phones.
 */
const PillSelect = ({ label, value, onChange, options, disabled }) => (
  <div className="relative inline-flex h-7 items-center gap-1 rounded-lg border border-transparent pl-2 pr-1.5 text-[12px] font-medium text-ink-3 transition-colors hover:border-line hover:bg-surface-2 hover:text-ink has-[select:focus-visible]:border-accent has-[select:focus-visible]:text-ink has-[select:focus-visible]:ring-2 has-[select:focus-visible]:ring-accent/25 has-[select:disabled]:opacity-50">
    <span aria-hidden="true" className="whitespace-nowrap">{shortLabel(options, value)}</span>
    <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="absolute inset-0 h-full w-full cursor-pointer appearance-none opacity-0 outline-none disabled:cursor-not-allowed"
    >
      {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select>
  </div>
);

/** Narrow composers fold the workflow and model pickers into one popover. */
const AnswerOptions = ({ preferences, onChange, disabled }) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const buttonRef = useRef(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      setOpen(false);
      buttonRef.current?.focus();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown, true);
    };
  }, [open]);

  const summary = `${shortLabel(WORKFLOWS, preferences.workflow)} · ${shortLabel(MODELS, preferences.model)}`;
  const selectClass = 'mt-1 block h-9 w-full rounded-lg border border-line bg-surface-2 px-2.5 text-[13px] font-medium text-ink outline-none focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/25 disabled:opacity-50';
  return (
    <div ref={rootRef} className="relative min-w-0">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`Answer options: ${summary}`}
        className="inline-flex h-7 min-w-0 max-w-full items-center gap-1 rounded-lg border border-transparent px-1.5 text-[12px] font-medium text-ink-3 transition-colors hover:border-line hover:bg-surface-2 hover:text-ink aria-expanded:border-line aria-expanded:bg-surface-2 aria-expanded:text-ink"
      >
        <SlidersHorizontal className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span className="truncate">{summary}</span>
      </button>
      {open && (
        <div
          id={panelId}
          role="group"
          aria-label="Answer options"
          className="absolute bottom-full left-0 z-30 mb-2 w-[min(16rem,calc(100vw-3rem))] animate-fade-in space-y-3 rounded-xl border border-line bg-surface p-3 shadow-[var(--shadow-lift)]"
        >
          <label className="block text-[11.5px] font-medium text-ink-3">
            Workflow
            <select value={preferences.workflow} disabled={disabled} onChange={(event) => onChange({ workflow: event.target.value })} className={selectClass}>
              {WORKFLOWS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label className="block text-[11.5px] font-medium text-ink-3">
            Answer model
            <select value={preferences.model} disabled={disabled} onChange={(event) => onChange({ model: event.target.value })} className={selectClass}>
              {MODELS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <p className="text-[11px] leading-snug text-ink-3">Detailed answers take longer but reason across more files.</p>
        </div>
      )}
    </div>
  );
};

const Composer = forwardRef(({ onSubmit, onStop, busy, disabled, disabledReason }, ref) => {
  const [text, setText] = useState('');
  const [preferences, setPreferences] = useState(readPreferences);
  const textareaRef = useRef(null);
  const composingRef = useRef(false);
  const hintId = useId();
  const historyLoading = useStore((state) => state.isHistoryLoading);
  const draft = useStore((state) => state.composerDraft);
  // Phones and tablets have no Shift+Enter: Enter adds a line and the send
  // button sends.
  const touchKeyboard = useMediaQuery('(hover: none) and (pointer: coarse)');

  const focusAtEnd = () => window.setTimeout(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.focus();
    const end = node.value.length;
    node.setSelectionRange?.(end, end);
  }, 0);

  useImperativeHandle(ref, () => ({
    focus: () => textareaRef.current?.focus(),
    setText: (value, mode) => {
      setText(value);
      if (mode) setPreferences((current) => ({ ...current, mode }));
      focusAtEnd();
    },
  }), []);

  // Drafts sent from elsewhere in the chat (follow-ups, "edit question").
  useEffect(() => {
    if (!draft) return;
    setText(draft.text);
    if (draft.mode === 'ask' || draft.mode === 'edit') setPreferences((current) => ({ ...current, mode: draft.mode }));
    focusAtEnd();
  }, [draft?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    try {
      window.localStorage.setItem(PREFERENCES_KEY, JSON.stringify(preferences));
    } catch {
      // Storage may be unavailable.
    }
  }, [preferences]);

  // Auto-grow up to ~10 lines.
  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = `${Math.min(node.scrollHeight, 240)}px`;
  }, [text]);

  const editing = preferences.mode === 'edit';
  const length = text.trim().length;
  const tooLong = length > MAX_QUESTION_LENGTH;
  const canSend = Boolean(length) && !tooLong && !busy && !disabled;

  const submit = () => {
    const value = text.trim();
    if (!canSend) return;
    onSubmit(value, {
      workflow: editing ? 'editing' : preferences.workflow,
      modelProfile: editing ? 'code' : preferences.model,
    });
    setText('');
    // Let the reader watch the answer instead of the on-screen keyboard.
    if (touchKeyboard) textareaRef.current?.blur();
  };

  const recallLastQuestion = () => {
    const messages = useStore.getState().messages;
    const last = [...messages].reverse().find((message) => message.role === 'user');
    if (!last) return false;
    setText(last.content);
    focusAtEnd();
    return true;
  };

  const onKeyDown = (event) => {
    if (composingRef.current || event.nativeEvent.isComposing) return;
    if (event.key === 'Escape' && busy) {
      // Stop the answer; keep Escape from also closing the sources panel.
      event.preventDefault();
      event.stopPropagation();
      onStop?.();
      return;
    }
    if (event.key === 'ArrowUp' && !text && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey) {
      if (recallLastQuestion()) event.preventDefault();
      return;
    }
    if (event.key !== 'Enter' || event.shiftKey) return;
    const explicit = event.metaKey || event.ctrlKey;
    if (touchKeyboard && !explicit) return;
    event.preventDefault();
    submit();
  };

  const waitingReason = disabled
    ? (historyLoading ? 'Loading this conversation…' : (disabledReason || 'Indexing is in progress. You can ask questions as soon as keyword search is ready.'))
    : '';

  let placeholder = 'Ask how something works or where it lives';
  if (disabled) placeholder = historyLoading ? 'Ask about this codebase' : 'Questions open once the index is ready';
  else if (editing) placeholder = 'Describe a change, or paste a GitHub issue URL';

  return (
    <div className="@container mx-auto w-full max-w-3xl">
      <div className={`rounded-2xl border bg-surface shadow-[var(--shadow-soft)] transition-[border-color,box-shadow] focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/25 ${editing ? 'border-accent/40' : 'border-line'} ${disabled ? 'bg-surface-2/60' : ''}`}>
        {waitingReason && (
          <p className="flex items-start gap-2 px-4 pt-3 text-[12.5px] leading-snug text-ink-2" role="status">
            {historyLoading
              ? <Spinner className="mt-px h-3.5 w-3.5 shrink-0 text-accent" />
              : <Clock className="mt-px h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />}
            <span>{waitingReason}</span>
          </p>
        )}
        <textarea
          ref={textareaRef}
          value={text}
          rows={1}
          disabled={disabled}
          onChange={(event) => setText(event.target.value)}
          onCompositionStart={() => { composingRef.current = true; }}
          onCompositionEnd={() => { composingRef.current = false; }}
          onKeyDown={onKeyDown}
          enterKeyHint={touchKeyboard ? 'enter' : 'send'}
          aria-label={editing ? 'Describe the code change' : 'Ask about this codebase'}
          aria-describedby={hintId}
          placeholder={placeholder}
          className="scroll-thin block max-h-60 min-h-[3.25rem] w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-[14.5px] leading-relaxed text-ink outline-none placeholder:text-ink-3 disabled:cursor-not-allowed"
        />
        <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5 pt-1">
          <div className="flex min-w-0 flex-1 items-center gap-1">
            <Segmented
              size="xs"
              label="Mode"
              className="shrink-0"
              value={preferences.mode}
              onChange={(mode) => {
                setPreferences((current) => ({ ...current, mode }));
                textareaRef.current?.focus();
              }}
              options={[
                { value: 'ask', label: 'Ask', icon: MessageSquareText, title: 'Ask questions; answers cite exact files and lines' },
                { value: 'edit', label: 'Edit & PR', icon: GitPullRequest, title: 'Describe a change; review a validated diff, then open a pull request' },
              ]}
            />
            {editing ? (
              <span className="hidden min-w-0 truncate px-2 text-[11.5px] text-ink-3 @lg:inline">Writes a validated patch · you review the diff before any PR</span>
            ) : (
              <>
                <div className="hidden min-w-0 items-center gap-0.5 @md:flex">
                  <PillSelect label="Workflow" value={preferences.workflow} onChange={(workflow) => setPreferences((current) => ({ ...current, workflow }))} options={WORKFLOWS} disabled={busy} />
                  <PillSelect label="Answer model" value={preferences.model} onChange={(model) => setPreferences((current) => ({ ...current, model }))} options={MODELS} disabled={busy} />
                </div>
                <div className="min-w-0 @md:hidden">
                  <AnswerOptions preferences={preferences} disabled={busy} onChange={(patch) => setPreferences((current) => ({ ...current, ...patch }))} />
                </div>
              </>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {length > WARN_QUESTION_LENGTH && (
              <span className={`font-mono text-[11px] tabular-nums ${tooLong ? 'font-semibold text-danger' : 'text-ink-3'}`} aria-live="polite">
                {length.toLocaleString()}/{MAX_QUESTION_LENGTH.toLocaleString()}
              </span>
            )}
            {busy ? (
              <button type="button" onClick={onStop} title="Stop generating (Esc)" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink text-canvas transition-opacity hover:opacity-85" aria-label="Stop generating">
                <Square className="h-3 w-3 fill-current" aria-hidden="true" />
              </button>
            ) : (
              <button
                type="button"
                onClick={submit}
                disabled={!canSend}
                title={editing ? 'Generate change (Enter)' : 'Send question (Enter)'}
                className={`flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg bg-accent text-accent-ink transition-colors hover:bg-accent-strong disabled:bg-surface-3 disabled:text-ink-3 ${editing ? 'w-8 @md:w-auto @md:px-3' : 'w-8'}`}
                aria-label={editing ? 'Generate change' : 'Send question'}
              >
                <ArrowUp className="h-4 w-4" aria-hidden="true" />
                {editing && <span className="hidden text-[12.5px] font-semibold @md:inline">Generate</span>}
              </button>
            )}
          </div>
        </div>
      </div>
      <p id={hintId} className={`mt-2 min-h-4 items-center justify-center gap-1 text-center text-[11px] text-ink-3 ${editing || tooLong ? 'flex' : 'hidden sm:flex'}`}>
        {tooLong ? (
          <span className="text-danger">Shorten this to {MAX_QUESTION_LENGTH.toLocaleString()} characters to send it.</span>
        ) : busy ? (
          <span className="hidden sm:inline">Working on it · press <Kbd>Esc</Kbd> to stop</span>
        ) : editing ? (
          <span>Writes and validates a patch · you review the diff before any pull request</span>
        ) : touchKeyboard ? (
          <span>Answers cite exact files and lines</span>
        ) : (
          <>
            <Kbd>Enter</Kbd><span>to send</span><span aria-hidden="true" className="px-1">·</span>
            <Kbd>Shift</Kbd><span>+</span><Kbd>Enter</Kbd><span>new line</span>
            <span aria-hidden="true" className="px-1">·</span><span>answers cite exact files and lines</span>
          </>
        )}
      </p>
    </div>
  );
});

Composer.displayName = 'Composer';
export default Composer;
