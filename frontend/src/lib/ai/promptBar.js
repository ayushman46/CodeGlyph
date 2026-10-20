/*
 * Pure logic of the Prompt Bar (components/ai/PromptBar.jsx), a port of the
 * Honest UI Prompt Bar. Kept free of React and the DOM so it runs under
 * `node --test` (promptBar.test.mjs).
 */

/**
 * @typedef {object} PromptBarLabels
 * @property {string} prompt Accessible name of the text area.
 * @property {string} send Name of the submit button.
 * @property {string} stop Name of the same button while a reply is in progress.
 * @property {string} attach Name of the paperclip button.
 * @property {string} attachments Name of the attachment chip list.
 * @property {(name: string) => string} removeAttachment Name of a chip's remove button.
 * @property {string} model Prefix of the model button ("Model: <selection>").
 * @property {string} mentions Name of the `@` suggestion list.
 * @property {string} commands Name of the `/` suggestion list.
 * @property {(query: string) => string} noMatches Shown (and announced) when nothing matches.
 * @property {(count: number) => string} suggestionCount Announced while the list is open.
 */

/**
 * Every string the Prompt Bar shows or announces. Pass a partial object as
 * `labels` to translate some of them; the rest fall back to these.
 *
 * @type {PromptBarLabels}
 */
export const defaultPromptBarLabels = {
  prompt: 'Prompt',
  send: 'Send',
  stop: 'Stop',
  attach: 'Attach files',
  attachments: 'Attachments',
  removeAttachment: (name) => `Remove ${name}`,
  model: 'Model',
  mentions: 'Sources',
  commands: 'Commands',
  noMatches: (query) => `No matches for “${query}”.`,
  suggestionCount: (count) => (count === 1 ? '1 suggestion' : `${count} suggestions`),
};

/** The text area grows with its content up to this height (px), then scrolls. */
export const MAX_INPUT_HEIGHT = 160;

/**
 * @typedef {object} PromptBarSuggestion
 * @property {'mention'|'command'} kind `@` opens mentions, `/` opens commands.
 * @property {number} start Index of the trigger character in the text.
 * @property {string} query The typed word after the trigger, lower-cased.
 */

/*
 * The trigger must start a word (start of the text or after whitespace) and
 * the word must run up to the caret. A word is letters, digits, `_` and `-`;
 * after `@` it may also hold `.` and `/`, so a file path
 * (`@source/utils/normalize.ts`) can be typed out in full.
 */
const SUGGESTION_PATTERN = /(^|\s)(?:(@)([\w./-]*)|(\/)([\w-]*))$/;

/**
 * Finds an `@word` or `/word` that ends at the caret and starts a word.
 *
 * @param {string} text
 * @param {number} caret
 * @returns {PromptBarSuggestion | null}
 */
export function findSuggestion(text, caret) {
  const match = SUGGESTION_PATTERN.exec(text.slice(0, caret));
  if (!match) return null;
  const mention = match[2] === '@';
  return {
    kind: mention ? 'mention' : 'command',
    start: match.index + match[1].length,
    query: (mention ? match[3] : match[5]).toLowerCase(),
  };
}

/**
 * The items a suggestion lists: mentions match anywhere in the label,
 * commands match from the start. Case does not matter.
 *
 * @template {{ label: string }} T
 * @param {readonly T[] | undefined} items
 * @param {PromptBarSuggestion | null} suggestion
 * @returns {T[]}
 */
export function matchSuggestions(items, suggestion) {
  if (!suggestion || !items?.length) return [];
  return items.filter((item) => {
    const label = item.label.toLowerCase();
    return suggestion.kind === 'mention' ? label.includes(suggestion.query) : label.startsWith(suggestion.query);
  });
}

/**
 * The active option after ArrowDown / ArrowUp, wrapping at both ends. A
 * stale index (the list got shorter) counts from the last option.
 *
 * @param {number} current
 * @param {number} count number of options (> 0)
 * @param {'ArrowDown'|'ArrowUp'} key
 * @returns {number}
 */
export function stepActiveIndex(current, count, key) {
  const step = key === 'ArrowDown' ? 1 : count - 1;
  return (Math.min(current, count - 1) + step) % count;
}

/**
 * Replaces the typed `@que` / `/que` (from the trigger up to the caret) with
 * the chosen label and a trailing space; the text after the caret is kept.
 *
 * @param {string} value
 * @param {number} caret
 * @param {PromptBarSuggestion} suggestion
 * @param {string} label
 * @returns {{ value: string, caret: number }}
 */
export function insertSuggestion(value, caret, suggestion, label) {
  const trigger = suggestion.kind === 'mention' ? '@' : '/';
  const inserted = `${trigger}${label} `;
  return {
    value: value.slice(0, suggestion.start) + inserted + value.slice(caret),
    caret: suggestion.start + inserted.length,
  };
}
