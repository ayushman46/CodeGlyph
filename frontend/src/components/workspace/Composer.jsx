import {
  ChevronDown, ClipboardCheck, Clock, FileCode2, FileText, GitPullRequest, GraduationCap, Layers,
  MessageCircleQuestion, MessageSquareText, ShieldCheck, Sparkles, Users, Zap,
} from 'lucide-react';
import { forwardRef, useCallback, useEffect, useId, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { searchRepository } from '../../api/client';
import useMediaQuery from '../../hooks/useMediaQuery';
import { findSuggestion } from '../../lib/ai/promptBar';
import useStore from '../../store/useStore';
import { PromptBar } from '../ai/PromptBar';
import Button from '../ui/Button';
import Kbd from '../ui/Kbd';
import { MenuPopup, MenuRadioGroup, MenuRadioItem, MenuRoot, MenuTrigger } from '../ui/Menu';
import Segmented from '../ui/Segmented';
import Spinner from '../ui/Spinner';

/*
 * The workspace composer, built on the Prompt Bar port (components/ai/PromptBar.jsx):
 * - Ask vs Edit & PR stays a radio group, next to Send;
 * - the answer workflows are `/` commands and a menu beside the model menu;
 * - the answer model (Fast / Detailed) is the Prompt Bar's model menu;
 * - `@` lists the repository's indexed files and inserts the path.
 */

/** `command` is the one-word `/` command; `short` labels the menu button. */
export const WORKFLOWS = [
  { value: 'general', label: 'General question', short: 'General', command: 'general', icon: MessageCircleQuestion },
  { value: 'onboarding', label: 'New engineer onboarding', short: 'Onboarding', command: 'onboarding', icon: GraduationCap },
  { value: 'architecture', label: 'Architecture deep dive', short: 'Architecture', command: 'architecture', icon: Layers },
  { value: 'security', label: 'Security review', short: 'Security', command: 'security', icon: ShieldCheck },
  { value: 'contributor', label: 'Open-source contributor', short: 'Contributor', command: 'contributor', icon: Users },
  { value: 'due_diligence', label: 'Technical due diligence', short: 'Due diligence', command: 'diligence', icon: ClipboardCheck },
];

const WORKFLOW_COMMANDS = WORKFLOWS.map(({ value, label, command, icon: Icon }) => ({
  id: value, label: command, description: label, icon: <Icon />,
}));

/** The answer models, as Prompt Bar models (ids are the API's model_profile). */
const MODELS = [
  { id: 'fast', label: 'Fast', description: 'Super 120B', icon: <Zap /> },
  { id: 'detailed', label: 'Detailed', description: 'Ultra 550B', icon: <Sparkles /> },
];

// The API rejects longer questions; count down before that happens.
const MAX_QUESTION_LENGTH = 8000;
const WARN_QUESTION_LENGTH = 7000;

const PREFERENCES_KEY = 'ci-composer';

// `@` file suggestions: at most this many are offered; code search runs
// (debounced) once the typed word has two characters.
export const MENTION_LIMIT = 50;
const MENTION_SEARCH_MIN = 2;
const MENTION_SEARCH_DELAY_MS = 150;
const MENTION_POOL_LIMIT = 600;

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

const unique = (values) => [...new Set(values.filter(Boolean))];

const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Applies workflow commands in a question: a leading `/command` (typed or
 * picked) and any command picked from the list elsewhere in the text. The
 * command words are removed from the question; the last one wins.
 *
 * @param {string} text the trimmed prompt
 * @param {string[]} [picked] command labels picked from the `/` list
 * @returns {{ question: string, workflow: string | null }}
 */
export const applyWorkflowCommands = (text, picked = []) => {
  let question = text;
  let workflow = null;
  const leading = /^\/([\w-]+)(?=\s|$)/.exec(question);
  const lead = leading && WORKFLOWS.find((item) => item.command === leading[1].toLowerCase());
  if (lead) {
    workflow = lead.value;
    question = question.slice(leading[0].length);
  }
  picked.forEach((label) => {
    const item = WORKFLOWS.find((option) => option.command === label);
    if (!item) return;
    const match = new RegExp(`(^|\\s)/${escapeRegExp(label)}(?:[ \\t]+|(?=\\s)|$)`, 'i').exec(question);
    if (!match) return;
    question = question.slice(0, match.index + match[1].length) + question.slice(match.index + match[0].length);
    workflow = item.value;
  });
  return { question: question.trim(), workflow };
};

/**
 * Orders indexed paths for the `@` list and caps it. Paths containing the
 * query come first (file name prefix, then file name, then a folder, then
 * anywhere; shorter paths first); the rest follow so the list never empties
 * (the Prompt Bar hides non-matches and says "No matches").
 *
 * @param {string[]} paths
 * @param {string} [query]
 * @param {number} [limit]
 * @returns {string[]}
 */
export const rankMentionPaths = (paths, query = '', limit = MENTION_LIMIT) => {
  const needle = query.toLowerCase();
  if (!needle) return paths.slice(0, limit);
  const matched = [];
  const rest = [];
  paths.forEach((path, order) => {
    const lower = path.toLowerCase();
    if (!lower.includes(needle)) {
      rest.push(path);
      return;
    }
    const name = lower.slice(lower.lastIndexOf('/') + 1);
    let rank = 3;
    if (name.startsWith(needle)) rank = 0;
    else if (name.includes(needle)) rank = 1;
    else if (lower.startsWith(needle) || lower.includes(`/${needle}`)) rank = 2;
    matched.push({ path, rank, order });
  });
  matched.sort((a, b) => a.rank - b.rank || a.path.length - b.path.length || a.order - b.order);
  return [...matched.map((item) => item.path), ...rest].slice(0, limit);
};

/**
 * What code search looks for while an `@` mention is typed: the word itself,
 * or for a path the file name being typed without its extension
 * (`source/utils/normalize.ts` → `normalize`; `src/` → `src`).
 *
 * @param {string} query the typed mention word (lower-case)
 * @returns {string}
 */
export const mentionSearchTerm = (query = '') => {
  if (!/[/.]/.test(query)) return query;
  const segment = query.split('/').filter(Boolean).pop() ?? '';
  const stem = segment.split('.')[0];
  return stem.length >= MENTION_SEARCH_MIN ? stem : segment;
};

const DOCUMENT_FILE = /\.(md|mdx|markdown|txt|rst|adoc)$/i;
const mentionIcon = (path) => (DOCUMENT_FILE.test(path) || !/\.[^/]+$/.test(path) ? <FileText /> : <FileCode2 />);

/** Files the conversation cited, newest first, as one string (a stable store selection). */
const citedPathsKey = (messages) => {
  const seen = new Set();
  for (let index = messages.length - 1; index >= 0 && seen.size < 40; index -= 1) {
    (messages[index].citations || []).forEach((citation) => {
      if (citation?.file_path) seen.add(citation.file_path);
    });
  }
  return [...seen].join('\n');
};

const NO_SEARCHES = { repo: null, queries: [], paths: [] };

/**
 * The `@` suggestions: the repository's indexed files, loaded lazily on the
 * first `@` (the overview's key, root and starter-question files, the files
 * this conversation cited) and widened by code search as the word grows.
 *
 * @param {string | null} repoName
 * @param {{ kind: 'mention' | 'command', query: string } | null} suggestion the word at the caret
 */
function useFileMentions(repoName, suggestion) {
  const loadOverview = useStore((state) => state.loadOverview);
  const overview = useStore((state) => (repoName ? state.overviews[repoName]?.data : null));
  const cited = useStore((state) => citedPathsKey(state.messages));
  const [found, setFound] = useState(NO_SEARCHES);
  const [pendingQuery, setPendingQuery] = useState(null);
  const active = Boolean(repoName && suggestion?.kind === 'mention');
  const query = active ? suggestion.query : '';
  // Code search matches file text, not paths: for a typed path search the
  // file name being typed (`source/utils/norm` searches `norm`); the list
  // then keeps the paths that contain the whole typed path.
  const term = mentionSearchTerm(query);
  const searches = found.repo === repoName ? found : NO_SEARCHES;
  const needsSearch = active && term.length >= MENTION_SEARCH_MIN && !searches.queries.includes(term);

  useEffect(() => {
    if (active) void loadOverview(repoName);
  }, [active, repoName, loadOverview]);

  useEffect(() => {
    if (!needsSearch) return undefined;
    const controller = new AbortController();
    setPendingQuery(term);
    const timer = window.setTimeout(async () => {
      let paths = [];
      try {
        const response = await searchRepository(repoName, term, { signal: controller.signal });
        paths = (response.data?.results || []).map((result) => result.file_path);
      } catch {
        if (controller.signal.aborted) return;
      }
      setFound((current) => {
        const base = current.repo === repoName ? current : { ...NO_SEARCHES, repo: repoName };
        return {
          repo: repoName,
          queries: [...base.queries, term].slice(-200),
          paths: unique([...base.paths, ...paths]).slice(-MENTION_POOL_LIMIT),
        };
      });
      setPendingQuery((current) => (current === term ? null : current));
    }, MENTION_SEARCH_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [needsSearch, repoName, term]);

  const items = useMemo(() => {
    const pool = unique([
      ...(cited ? cited.split('\n') : []),
      ...(overview?.key_files || []),
      ...(overview?.suggested_question_details || []).flatMap((detail) => detail?.paths || []),
      ...(overview?.root_files || []),
      ...searches.paths,
    ]);
    return rankMentionPaths(pool, query).map((path) => ({ id: path, label: path, icon: mentionIcon(path) }));
  }, [cited, overview, searches.paths, query]);

  return { items, searching: needsSearch && pendingQuery === term };
}

/** The workflow menu: the same `/` commands as a radio menu beside the model menu. */
const WorkflowMenu = ({ value, onChange, disabled }) => {
  const selected = WORKFLOWS.find((item) => item.value === value) || WORKFLOWS[0];
  const Icon = selected.icon;
  return (
    <MenuRoot>
      <MenuTrigger disabled={disabled} render={<Button variant="link" size="sm" className="max-w-full min-w-0" />}>
        <span className="sr-only">Workflow: </span>
        <Icon aria-hidden="true" />
        <span className="min-w-0 truncate @max-sm:sr-only">{selected.short}</span>
        <ChevronDown aria-hidden="true" className="size-3 text-ink-3" />
      </MenuTrigger>
      <MenuPopup align="start" className="min-w-56">
        <MenuRadioGroup value={selected.value} onValueChange={(next) => onChange(String(next))}>
          {WORKFLOWS.map((item) => {
            const ItemIcon = item.icon;
            return (
              <MenuRadioItem key={item.value} value={item.value}>
                <span className="flex min-w-0 items-center justify-between gap-3">
                  <span className="flex min-w-0 items-center gap-1">
                    <ItemIcon aria-hidden="true" />
                    <span className="min-w-0 truncate">{item.label}</span>
                  </span>
                  <span className="shrink-0 font-mono text-[11px] text-ink-2">/{item.command}</span>
                </span>
              </MenuRadioItem>
            );
          })}
        </MenuRadioGroup>
      </MenuPopup>
    </MenuRoot>
  );
};

const Separator = () => <span aria-hidden="true" className="px-1">·</span>;

/*
 * Layout hooks on the Prompt Bar's slots:
 * - the actions join the controls row, so the workflow menu sits beside the
 *   model menu and the mode switch beside Send, in reading order;
 * - a suggestion's last text (a file path, which has no description) may
 *   wrap instead of running past a phone-width list.
 */
const PROMPT_BAR_LAYOUT = [
  '[&_[data-slot=prompt-bar-actions]]:contents',
  '[&_[data-slot=prompt-bar-suggestions]_[role=option]>span:last-child]:min-w-0',
  '[&_[data-slot=prompt-bar-suggestions]_[role=option]>span:last-child]:shrink',
  '[&_[data-slot=prompt-bar-suggestions]_[role=option]>span:last-child]:[overflow-wrap:anywhere]',
].join(' ');

const Composer = forwardRef(({ onSubmit, onStop, busy, disabled, disabledReason }, ref) => {
  const [text, setText] = useState('');
  const [preferences, setPreferences] = useState(readPreferences);
  // The `@word` / `/word` at the caret, tracked like the Prompt Bar does.
  const [suggestion, setSuggestion] = useState(null);
  const [announcement, setAnnouncement] = useState('');
  const rootRef = useRef(null);
  const keepDraftRef = useRef(false);
  const pickedCommandsRef = useRef([]);
  const hintId = useId();
  const historyLoading = useStore((state) => state.isHistoryLoading);
  const draft = useStore((state) => state.composerDraft);
  const repoName = useStore((state) => state.selectedRepo);
  const stopQuery = useStore((state) => state.stopQuery);
  // Phones and tablets have no Shift+Enter: Enter adds a line and the send
  // button sends.
  const touchKeyboard = useMediaQuery('(hover: none) and (pointer: coarse)');
  const { items: mentions, searching } = useFileMentions(repoName, suggestion);

  const editing = preferences.mode === 'edit';
  const length = text.trim().length;
  const tooLong = length > MAX_QUESTION_LENGTH;
  const stop = onStop || stopQuery;

  const getInput = useCallback(() => rootRef.current?.querySelector('[data-slot="prompt-bar-input"]') ?? null, []);

  const focusAtEnd = useCallback(() => window.setTimeout(() => {
    const node = getInput();
    if (!node) return;
    node.focus();
    const end = node.value.length;
    node.setSelectionRange?.(end, end);
  }, 0), [getInput]);

  useImperativeHandle(ref, () => ({
    focus: () => getInput()?.focus(),
    setText: (value, mode) => {
      setText(value);
      if (mode) setPreferences((current) => ({ ...current, mode }));
      focusAtEnd();
    },
  }), [getInput, focusAtEnd]);

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

  // The hint row describes the text area; touch keyboards show "return".
  useEffect(() => {
    const node = getInput();
    if (!node) return;
    node.setAttribute('aria-describedby', hintId);
    node.setAttribute('enterkeyhint', touchKeyboard ? 'enter' : 'send');
  }, [getInput, hintId, touchKeyboard]);

  const syncSuggestion = useCallback(() => {
    const node = getInput();
    if (!node) return;
    const next = findSuggestion(node.value, node.selectionStart ?? node.value.length);
    setSuggestion((current) => (current?.kind === next?.kind && current?.query === next?.query ? current : next));
  }, [getInput]);

  // After the Prompt Bar has placed the caret (its layout effect runs first).
  useEffect(syncSuggestion, [text, syncSuggestion]);

  const chooseWorkflow = (workflow, { announce = false } = {}) => {
    setPreferences((current) => (current.workflow === workflow ? current : { ...current, workflow }));
    const item = WORKFLOWS.find((option) => option.value === workflow);
    if (announce && item) setAnnouncement(`Workflow: ${item.label}`);
  };

  const changeText = (next) => {
    // A refused send (too long) keeps the draft the Prompt Bar would clear.
    if (keepDraftRef.current) {
      keepDraftRef.current = false;
      if (next === '') return;
    }
    if (next === '') pickedCommandsRef.current = [];
    setText(next);
  };

  const submit = ({ text: value, model }) => {
    if (value.length > MAX_QUESTION_LENGTH) {
      // The Prompt Bar would send and clear; keep the draft and say why.
      keepDraftRef.current = true;
      setAnnouncement(`Shorten this to ${MAX_QUESTION_LENGTH.toLocaleString()} characters to send it.`);
      return;
    }
    const parsed = editing ? { question: value, workflow: null } : applyWorkflowCommands(value, pickedCommandsRef.current);
    pickedCommandsRef.current = [];
    if (parsed.workflow) chooseWorkflow(parsed.workflow, { announce: true });
    // A bare `/command` only switches the workflow.
    if (!parsed.question) return;
    onSubmit(parsed.question, {
      workflow: editing ? 'editing' : (parsed.workflow || preferences.workflow),
      modelProfile: editing ? 'code' : (model || preferences.model),
    });
    // Let the reader watch the answer instead of the on-screen keyboard.
    if (touchKeyboard) getInput()?.blur();
  };

  const selectItem = (item, kind) => {
    if (kind !== 'command') return;
    pickedCommandsRef.current = [...pickedCommandsRef.current.filter((label) => label !== item.label), item.label];
    chooseWorkflow(item.id, { announce: true });
  };

  const recallLastQuestion = () => {
    const messages = useStore.getState().messages;
    const last = [...messages].reverse().find((message) => message.role === 'user');
    if (!last) return false;
    setText(last.content);
    focusAtEnd();
    return true;
  };

  const onKeyDownCapture = (event) => {
    if (!touchKeyboard || event.target !== getInput()) return;
    if (event.key !== 'Enter' || event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.nativeEvent.isComposing || rootRef.current?.querySelector('[data-slot="prompt-bar-suggestions"] [role="listbox"]')) return;
    // On a touch keyboard Enter adds a line: keep it from the Prompt Bar's send.
    event.stopPropagation();
  };

  const onKeyDown = (event) => {
    if (event.target !== getInput() || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (event.key === 'Escape') {
      // The Prompt Bar closed its suggestions: nothing else (the sources panel) closes.
      if (event.defaultPrevented) {
        event.stopPropagation();
        return;
      }
      if (busy) {
        // Stop the answer; keep Escape from also closing the sources panel.
        event.preventDefault();
        event.stopPropagation();
        stop?.();
      }
      return;
    }
    if (event.key === 'ArrowUp' && !event.defaultPrevented && !text && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey) {
      if (recallLastQuestion()) event.preventDefault();
    }
  };

  const labels = useMemo(() => ({
    prompt: editing ? 'Describe the code change' : 'Ask about this codebase',
    send: editing ? 'Generate change' : 'Send question',
    stop: 'Stop generating',
    model: 'Answer model',
    mentions: 'Indexed files',
    commands: 'Workflows',
    noMatches: (query) => (suggestion?.kind === 'mention' && searching
      ? `Searching files for “${query}”…`
      : `No matches for “${query}”.`),
  }), [editing, suggestion?.kind, searching]);

  const waitingReason = disabled
    ? (historyLoading ? 'Loading this conversation…' : (disabledReason || 'Indexing is in progress. You can ask questions as soon as keyword search is ready.'))
    : '';

  let placeholder = 'Ask how something works or where it lives';
  if (disabled) placeholder = historyLoading ? 'Ask about this codebase' : 'Questions open once the index is ready';
  else if (editing) placeholder = 'Describe a change, or paste a GitHub issue URL';

  return (
    <div ref={rootRef} className="@container mx-auto w-full max-w-3xl">
      {waitingReason && (
        <p className="mb-2 flex items-start gap-2 px-1 text-[12.5px] leading-snug text-ink-2" role="status">
          {historyLoading
            ? <Spinner className="mt-px h-3.5 w-3.5 shrink-0 text-accent" />
            : <Clock className="mt-px h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />}
          <span>{waitingReason}</span>
        </p>
      )}
      <PromptBar
        data-mode={preferences.mode}
        className={PROMPT_BAR_LAYOUT}
        value={text}
        onValueChange={changeText}
        onSubmit={submit}
        placeholder={placeholder}
        disabled={disabled}
        onStop={busy ? stop : undefined}
        mentions={mentions}
        commands={editing ? undefined : WORKFLOW_COMMANDS}
        onItemSelect={selectItem}
        models={editing ? undefined : MODELS}
        model={preferences.model}
        onModelChange={(model) => setPreferences((current) => ({ ...current, model }))}
        labels={labels}
        onSelect={syncSuggestion}
        onKeyDownCapture={onKeyDownCapture}
        onKeyDown={onKeyDown}
        actions={(
          <>
            {!editing && (
              <WorkflowMenu value={preferences.workflow} disabled={disabled} onChange={(workflow) => chooseWorkflow(workflow)} />
            )}
            <span className="ms-auto" />
            {length > WARN_QUESTION_LENGTH && (
              <span className={`px-1 font-mono text-[11px] tabular-nums ${tooLong ? 'font-semibold text-danger' : 'text-ink-3'}`} aria-live="polite">
                {length.toLocaleString()}/{MAX_QUESTION_LENGTH.toLocaleString()}
              </span>
            )}
            <Segmented
              size="sm"
              label="Mode"
              className="shrink-0 @max-sm:[&_svg]:hidden"
              value={preferences.mode}
              onChange={(mode) => {
                setPreferences((current) => ({ ...current, mode }));
                getInput()?.focus();
              }}
              // Inert with the rest of the dimmed field while the bar is disabled.
              options={[
                { value: 'ask', label: 'Ask', icon: MessageSquareText, title: 'Ask questions; answers cite exact files and lines', disabled },
                { value: 'edit', label: 'Edit & PR', icon: GitPullRequest, title: 'Describe a change; review a validated diff, then open a pull request', disabled },
              ]}
            />
          </>
        )}
      />
      <span role="status" className="sr-only">{announcement}</span>
      <p id={hintId} className={`mt-2 min-h-4 items-center justify-center gap-1 text-center text-[11px] text-ink-3 ${editing || tooLong ? 'flex' : 'hidden sm:flex'}`}>
        {tooLong ? (
          <span className="text-danger">Shorten this to {MAX_QUESTION_LENGTH.toLocaleString()} characters to send it.</span>
        ) : busy ? (
          <span className="hidden sm:inline">Working on it · press <Kbd>Esc</Kbd> to stop</span>
        ) : editing ? (
          <span>Writes and validates a patch · you review the diff before any pull request</span>
        ) : touchKeyboard ? (
          <>
            <Kbd>@</Kbd><span>files</span><Separator />
            <Kbd>/</Kbd><span>workflows</span><Separator />
            <span>answers cite exact files and lines</span>
          </>
        ) : (
          <>
            <Kbd>Enter</Kbd><span>to send</span><Separator />
            <Kbd>Shift</Kbd><span>+</span><Kbd>Enter</Kbd><span>new line</span><Separator />
            <Kbd>@</Kbd><span>files</span><Separator />
            <Kbd>/</Kbd><span>workflows</span>
            <span className="hidden items-center gap-1 @2xl:inline-flex"><Separator />answers cite exact files and lines</span>
          </>
        )}
      </p>
    </div>
  );
});

Composer.displayName = 'Composer';
export default Composer;
