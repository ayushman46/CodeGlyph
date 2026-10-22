import { ArrowUp, ChevronDown, File as FileIcon, Paperclip, Square, X as CloseIcon } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { cn } from '../../lib/cn';
import {
  MAX_INPUT_HEIGHT, defaultPromptBarLabels, findSuggestion, insertSuggestion, matchSuggestions, stepActiveIndex,
} from '../../lib/ai/promptBar';
import Button from '../ui/Button';
import { MenuPopup, MenuRadioGroup, MenuRadioItem, MenuRoot, MenuTrigger } from '../ui/Menu';

/*
 * Prompt Bar: a port of the Honest UI Prompt Bar
 * (https://ai.honestui.com/docs/components/prompt-bar) onto this app's
 * tokens. Collects a prompt with optional `@` mentions, `/` commands,
 * attachments and a model menu, and hands it to `onSubmit`. The pure logic
 * (trigger detection, matching, insertion) lives in lib/ai/promptBar.js.
 */

export { defaultPromptBarLabels };

/**
 * @typedef {object} PromptBarItem
 * @property {string} id
 * @property {string} label One typed word: no spaces (the list matches the word before the caret).
 * @property {string} [description]
 * @property {import('react').ReactNode} [icon]
 */

/**
 * @typedef {object} PromptBarModel
 * @property {string} id
 * @property {string} label
 * @property {string} [description]
 * @property {import('react').ReactNode} [icon]
 */

/**
 * @typedef {object} PromptBarSubmission
 * @property {string} text The prompt, trimmed.
 * @property {File[]} attachments
 * @property {string | undefined} model The selected model id, or undefined without `models`.
 */

/** @typedef {import('../../lib/ai/promptBar').PromptBarLabels} PromptBarLabels */

/**
 * @typedef {object} PromptBarOwnProps
 * @property {string} [value] Controlled text.
 * @property {string} [defaultValue='']
 * @property {(value: string) => void} [onValueChange]
 * @property {(submission: PromptBarSubmission) => void} onSubmit Called with the trimmed text; the bar then clears.
 * @property {string} [placeholder]
 * @property {boolean} [disabled=false] Blocks typing, attaching and sending (text kept); Stop still works.
 * @property {() => void} [onStop] Pass only while a reply is in progress: Send becomes Stop.
 * @property {PromptBarItem[]} [mentions] Listed after `@`; match anywhere in the label.
 * @property {PromptBarItem[]} [commands] Listed after `/`; match from the start of the label.
 * @property {(item: PromptBarItem, kind: 'mention' | 'command') => void} [onItemSelect]
 * @property {PromptBarModel[]} [models] Adds a model menu; the first model is selected by default.
 * @property {string} [model] Controlled model id.
 * @property {string} [defaultModel]
 * @property {(model: string) => void} [onModelChange]
 * @property {boolean} [allowAttachments=false] Adds a paperclip that opens the file picker.
 * @property {File[]} [attachments] Controlled attachments.
 * @property {(files: File[]) => void} [onAttachmentsChange]
 * @property {string} [accept] The file picker's `accept`.
 * @property {import('react').ReactNode} [actions] Extra controls before Send (e.g. a Context Meter).
 * @property {Partial<PromptBarLabels>} [labels] Every visible or announced string.
 * @property {string} [className]
 */

/**
 * @typedef {PromptBarOwnProps & Omit<import('react').FormHTMLAttributes<HTMLFormElement>, keyof PromptBarOwnProps>} PromptBarProps
 */

// --hui-font-size-regular (14/20) and --hui-font-size-small (12/16).
const bodyText = 'text-sm/5';
const smallText = 'text-xs';

/**
 * @template T
 * @param {T | undefined} controlled
 * @param {T} defaultValue
 * @param {((value: T) => void) | undefined} onChange
 * @returns {readonly [T, (next: T) => void]}
 */
function useControllableState(controlled, defaultValue, onChange) {
  const [uncontrolled, setUncontrolled] = useState(defaultValue);
  const value = controlled !== undefined ? controlled : uncontrolled;
  const setValue = (next) => {
    if (controlled === undefined) setUncontrolled(next);
    onChange?.(next);
  };
  return [value, setValue];
}

/** The suggestions list's own cap (max-h-64) and its gap above the field (mb-2). */
const SUGGESTIONS_MAX_HEIGHT = 256;
const SUGGESTIONS_GAP = 8;
const SUGGESTIONS_MIN_HEIGHT = 72;

/**
 * Room above the field for the suggestions list on a short viewport: up to
 * the viewport's top, the top of a clipping ancestor, or the bottom of a
 * sticky or fixed bar (a site header) before it, whichever is lowest.
 *
 * @param {HTMLElement} form
 * @returns {number}
 */
function roomAboveField(form) {
  const rect = form.getBoundingClientRect();
  let boundary = 0;
  for (let node = form; node && node !== document.body; node = node.parentElement) {
    const parent = node.parentElement;
    if (parent && parent !== document.body && getComputedStyle(parent).overflowY !== 'visible') {
      boundary = Math.max(boundary, parent.getBoundingClientRect().top);
    }
    for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
      const { position } = getComputedStyle(sibling);
      if (position !== 'sticky' && position !== 'fixed') continue;
      const bar = sibling.getBoundingClientRect();
      if (bar.bottom <= rect.top && bar.right > rect.left && bar.left < rect.right) boundary = Math.max(boundary, bar.bottom);
    }
  }
  return rect.top - boundary - SUGGESTIONS_GAP * 2;
}

/**
 * @param {PromptBarProps} props
 */
export function PromptBar({
  value: valueProp,
  defaultValue = '',
  onValueChange,
  onSubmit,
  placeholder,
  disabled = false,
  onStop,
  mentions,
  commands,
  onItemSelect,
  models,
  model: modelProp,
  defaultModel,
  onModelChange,
  allowAttachments = false,
  attachments: attachmentsProp,
  onAttachmentsChange,
  accept,
  actions,
  labels: labelsProp,
  className,
  ...props
}) {
  const labels = useMemo(() => ({ ...defaultPromptBarLabels, ...labelsProp }), [labelsProp]);
  const [value, setValue] = useControllableState(valueProp, defaultValue, onValueChange);
  const [model, setModel] = useControllableState(modelProp, defaultModel ?? models?.[0]?.id ?? '', onModelChange);
  const [attachments, setAttachments] = useControllableState(attachmentsProp, [], onAttachmentsChange);
  const [caret, setCaret] = useState(value.length);
  const [activeIndex, setActiveIndex] = useState(0);
  // Escape closes the suggestions until the text changes again.
  const [dismissedAt, setDismissedAt] = useState(/** @type {string | null} */ (null));
  const inputRef = useRef(/** @type {HTMLTextAreaElement | null} */ (null));
  const fileInputRef = useRef(/** @type {HTMLInputElement | null} */ (null));
  const sendButtonRef = useRef(/** @type {HTMLButtonElement | null} */ (null));
  const shouldRefocusInput = useRef(false);
  const pendingCaret = useRef(/** @type {number | null} */ (null));
  const formRef = useRef(/** @type {HTMLFormElement | null} */ (null));
  const suggestionsRef = useRef(/** @type {HTMLDivElement | null} */ (null));
  const listId = useId();

  const hasSuggestions = Boolean(mentions?.length || commands?.length);
  const suggestion = dismissedAt === value ? null : findSuggestion(value, caret);
  const source = suggestion?.kind === 'mention' ? mentions : commands;
  // `suggestion` is a new object on every render; the matches only depend
  // on its kind and query.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const matches = useMemo(() => matchSuggestions(source, suggestion), [source, suggestion?.kind, suggestion?.query]);
  const isMenuOpen = Boolean(suggestion && source?.length);
  const activeItem = matches[Math.min(activeIndex, matches.length - 1)];

  const measuredValue = useRef(value);
  useLayoutEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    // Grow with the text up to a limit, then scroll. Measuring a shrink needs
    // the height reset first, and that write costs the page a second full
    // layout, so only text that may have got shorter (a deletion, a cleared
    // or replaced draft) pays for it; the first render and typing do not.
    const mayShrink = !value.startsWith(measuredValue.current);
    measuredValue.current = value;
    if (mayShrink) input.style.height = '0px';
    const height = Math.min(input.scrollHeight, MAX_INPUT_HEIGHT);
    if (mayShrink || height !== input.clientHeight) input.style.height = `${height}px`;
    const overflowY = input.scrollHeight > MAX_INPUT_HEIGHT ? 'auto' : 'hidden';
    if (input.style.overflowY !== overflowY) input.style.overflowY = overflowY;
    if (pendingCaret.current !== null) {
      input.setSelectionRange(pendingCaret.current, pendingCaret.current);
      pendingCaret.current = null;
    }
  }, [value]);

  const changeValue = (next, nextCaret) => {
    setValue(next);
    setCaret(nextCaret);
    setActiveIndex(0);
    setDismissedAt(null);
  };

  /** @param {PromptBarItem} item */
  const insertItem = (item) => {
    if (!suggestion) return;
    const inserted = insertSuggestion(value, caret, suggestion, item.label);
    pendingCaret.current = inserted.caret;
    changeValue(inserted.value, inserted.caret);
    onItemSelect?.(item, suggestion.kind);
    inputRef.current?.focus();
  };

  const text = value.trim();
  const isStoppable = onStop !== undefined;
  const canSend = !disabled && !isStoppable && (text !== '' || attachments.length > 0);

  // Stop becomes Send again, often a disabled one. Hand focus to the field
  // once it can take it, unless the person has already moved somewhere else.
  // A focused button that turns disabled drops focus to <body> (the HTML
  // focus fixup rule, which Chromium applies before this effect runs), so
  // <body> counts as "still on the button".
  useEffect(() => {
    if (!shouldRefocusInput.current || isStoppable || disabled) return;
    shouldRefocusInput.current = false;
    const focused = document.activeElement;
    if (focused === sendButtonRef.current || focused === null || focused === document.body) inputRef.current?.focus();
  }, [isStoppable, disabled]);

  const send = () => {
    if (!canSend) return;
    onSubmit({ text, attachments, model: models?.length ? model : undefined });
    changeValue('', 0);
    setAttachments([]);
  };

  /** @param {import('react').KeyboardEvent<HTMLTextAreaElement>} event */
  const handleKeyDown = (event) => {
    // Enter confirms an in-progress IME composition; it must not send or
    // pick. Safari reports that Enter after compositionend with
    // isComposing false, so keyCode 229 ("IME is processing") counts too.
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (isMenuOpen) {
      if (event.key === 'Escape') {
        event.preventDefault();
        setDismissedAt(value);
        return;
      }
      if (matches.length > 0) {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          const key = event.key;
          setActiveIndex((current) => stepActiveIndex(current, matches.length, key));
          return;
        }
        if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
          event.preventDefault();
          if (activeItem) insertItem(activeItem);
          return;
        }
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  };

  /** @param {PromptBarItem} item */
  const optionId = (item) => `${listId}-${item.id}`;
  const selectedModel = models?.find((item) => item.id === model);
  const activeOptionId = isMenuOpen && activeItem ? optionId(activeItem) : undefined;

  // On a short viewport the list would run above the screen (or under a
  // sticky header): cap it to the room above the field while it is open.
  // Set on the element before the effect below keeps the active option in view.
  useLayoutEffect(() => {
    if (!isMenuOpen) return undefined;
    const measure = () => {
      const form = formRef.current;
      const list = suggestionsRef.current;
      if (!form || !list) return;
      const room = roomAboveField(form);
      list.style.maxHeight = room < SUGGESTIONS_MAX_HEIGHT ? `${Math.max(Math.floor(room), SUGGESTIONS_MIN_HEIGHT)}px` : '';
      // A shorter list can hide the option the arrow keys had reached.
      list.querySelector('[data-active]')?.scrollIntoView({ block: 'nearest' });
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [isMenuOpen, value]);

  // Keep the active option in view as the arrow keys move through a long
  // list (focus stays in the field, so the browser would not scroll it).
  useLayoutEffect(() => {
    if (!activeOptionId) return;
    document.getElementById(activeOptionId)?.scrollIntoView({ block: 'nearest' });
  }, [activeOptionId]);

  return (
    <form
      ref={formRef}
      data-slot="prompt-bar"
      data-disabled={disabled ? '' : undefined}
      className={cn('relative w-full min-w-0', className)}
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
      {...props}
    >
      {isMenuOpen && (
        <div
          ref={suggestionsRef}
          data-slot="prompt-bar-suggestions"
          className={cn(
            'absolute inset-x-0 bottom-full z-10 mb-2 max-h-64 overflow-y-auto rounded-xl border border-line bg-surface p-1 text-ink shadow-[var(--shadow-lift)] dark:border-line-strong',
            smallText,
          )}
        >
          {matches.length > 0 ? (
            <div id={listId} role="listbox" aria-label={suggestion?.kind === 'mention' ? labels.mentions : labels.commands}>
              {matches.map((item) => {
                const isActive = item === activeItem;
                return (
                  <div
                    key={item.id}
                    id={optionId(item)}
                    role="option"
                    aria-selected={isActive}
                    data-active={isActive ? '' : undefined}
                    // Keep focus in the field, so typing continues after a pick.
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseMove={() => setActiveIndex(matches.indexOf(item))}
                    onClick={() => insertItem(item)}
                    className="flex min-w-0 cursor-pointer items-center gap-2 rounded-lg px-2 py-2 data-active:bg-surface-2 forced-colors:data-active:outline-2 forced-colors:data-active:outline-offset-[-2px] forced-colors:data-active:outline-[Highlight]"
                  >
                    {item.icon && (
                      <span aria-hidden="true" className="flex size-4 shrink-0 items-center justify-center text-ink-2 [&_svg]:size-4">
                        {item.icon}
                      </span>
                    )}
                    <span className="shrink-0 font-medium">{suggestion?.kind === 'command' ? `/${item.label}` : item.label}</span>
                    {item.description && <span className="min-w-0 truncate text-ink-2">{item.description}</span>}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="m-0 px-2 py-2 text-ink-2">{labels.noMatches(suggestion?.query ?? '')}</p>
          )}
        </div>
      )}
      {hasSuggestions && (
        <span role="status" className="sr-only">
          {isMenuOpen ? (matches.length > 0 ? labels.suggestionCount(matches.length) : labels.noMatches(suggestion?.query ?? '')) : ''}
        </span>
      )}

      <div
        data-slot="prompt-bar-field"
        className="flex flex-col gap-1 rounded-2xl border border-line-strong bg-surface p-2 shadow-[var(--shadow-soft)] motion-safe:transition-interactive focus-within:border-accent in-data-disabled:opacity-60"
      >
        {attachments.length > 0 && (
          <ul role="list" aria-label={labels.attachments} data-slot="prompt-bar-attachments" className="m-0 flex list-none flex-wrap gap-1 p-0">
            {attachments.map((file, index) => (
              <li
                key={`${file.name}-${file.size}-${file.lastModified}-${index}`}
                className={cn('flex min-w-0 max-w-full items-center gap-1 rounded-lg bg-surface-2 py-0.5 ps-2 pe-0.5 text-ink-2', smallText)}
              >
                <FileIcon aria-hidden="true" className="size-3.5 shrink-0" />
                <span className="min-w-0 truncate">{file.name}</span>
                <Button
                  variant="link"
                  size="icon-sm"
                  disabled={disabled}
                  aria-label={labels.removeAttachment(file.name)}
                  onClick={() => {
                    setAttachments(attachments.filter((_, position) => position !== index));
                    inputRef.current?.focus();
                  }}
                >
                  <CloseIcon aria-hidden="true" className="size-3" />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <textarea
          ref={inputRef}
          rows={1}
          data-slot="prompt-bar-input"
          value={value}
          disabled={disabled}
          placeholder={placeholder}
          aria-label={labels.prompt}
          // A textarea cannot take the combobox role, so it stays a text box
          // that points at the list and its active option.
          {...(hasSuggestions ? {
            'aria-autocomplete': 'list',
            'aria-haspopup': 'listbox',
            'aria-controls': isMenuOpen && matches.length > 0 ? listId : undefined,
            'aria-activedescendant': isMenuOpen && activeItem ? optionId(activeItem) : undefined,
          } : {})}
          onChange={(event) => changeValue(event.target.value, event.target.selectionStart)}
          onSelect={(event) => setCaret(event.currentTarget.selectionStart)}
          onKeyDown={handleKeyDown}
          className={cn(
            'm-0 block w-full min-w-0 resize-none border-0 bg-transparent px-1 py-1 text-ink outline-none [overflow-wrap:anywhere] placeholder:text-ink-3 disabled:cursor-not-allowed',
            bodyText,
          )}
        />
        <div data-slot="prompt-bar-controls" className="flex min-w-0 flex-wrap items-center gap-1">
          {allowAttachments && (
            <>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                hidden
                accept={accept}
                tabIndex={-1}
                onChange={(event) => {
                  const added = Array.from(event.target.files ?? []);
                  if (added.length > 0) setAttachments([...attachments, ...added]);
                  event.target.value = '';
                  inputRef.current?.focus();
                }}
              />
              <Button variant="link" size="icon" disabled={disabled} aria-label={labels.attach} onClick={() => fileInputRef.current?.click()}>
                <Paperclip aria-hidden="true" />
              </Button>
            </>
          )}
          {models && models.length > 0 && (
            <MenuRoot>
              <MenuTrigger disabled={disabled} render={<Button variant="link" size="sm" className="max-w-full min-w-0" />}>
                <span className="sr-only">{labels.model}: </span>
                {selectedModel?.icon}
                <span className="min-w-0 truncate">{selectedModel?.label ?? labels.model}</span>
                <ChevronDown aria-hidden="true" className="size-3 text-ink-3" />
              </MenuTrigger>
              <MenuPopup align="start" className="min-w-48">
                <MenuRadioGroup value={model} onValueChange={(next) => setModel(String(next))}>
                  {models.map((item) => (
                    <MenuRadioItem key={item.id} value={item.id}>
                      <span className="flex min-w-0 items-center justify-between gap-3">
                        <span className="flex min-w-0 items-center gap-1">
                          {item.icon}
                          <span className="min-w-0 truncate">{item.label}</span>
                        </span>
                        {item.description && <span className="shrink-0 text-[11px] text-ink-2">{item.description}</span>}
                      </span>
                    </MenuRadioItem>
                  ))}
                </MenuRadioGroup>
              </MenuPopup>
            </MenuRoot>
          )}
          <div data-slot="prompt-bar-actions" className="ms-auto flex shrink-0 items-center gap-1">
            {actions}
            {/* One button for both, so focus stays put when a reply starts. */}
            <Button
              ref={sendButtonRef}
              variant="primary"
              type={isStoppable ? 'button' : 'submit'}
              size="icon"
              disabled={!isStoppable && !canSend}
              aria-label={isStoppable ? labels.stop : labels.send}
              onClick={onStop && ((event) => {
                // The button may be a submit button again by the time the
                // click finishes. Without this, stopping would also send the draft.
                event.preventDefault();
                shouldRefocusInput.current = true;
                onStop();
              })}
            >
              {isStoppable ? (
                <Square aria-hidden="true" fill="currentColor" className="size-3" />
              ) : (
                <ArrowUp aria-hidden="true" strokeWidth={2} className="size-4" />
              )}
            </Button>
          </div>
        </div>
      </div>
    </form>
  );
}

export default PromptBar;
