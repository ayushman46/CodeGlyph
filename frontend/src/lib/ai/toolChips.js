/**
 * Pure data mappers for the Tool Chips component (components/ai/ToolChips.jsx):
 * the code-edit run's backend `tool_calls` become `calls`, the proposal's
 * changed files become `changes` (with diff lines built from the real
 * {old, new} hunks), and a summary line says what matters ("4 steps · 7 files
 * changed", "Validation failed"). Dependency-free standard JavaScript so the
 * same code runs in the browser bundle and under `node --test`; inputs are
 * never mutated.
 */
import { diffLines, splitLines, toHunks, toRows } from '../diff.js';

/**
 * @typedef {'context' | 'added' | 'removed'} ToolLineKind
 * @typedef {{ text: string, kind?: ToolLineKind }} ToolLine
 * @typedef {'running' | 'done' | 'error'} ToolCallStatus
 * @typedef {{
 *   id: string,
 *   label: string,
 *   detail?: string,
 *   detailIsCode?: boolean,
 *   status?: ToolCallStatus,
 *   icon?: import('react').ReactNode,
 *   output?: ToolLine[],
 *   outputIsCode?: boolean,
 *   outcome?: 'warn' | 'skipped',
 * }} ToolCall
 * @typedef {{ path: string, added: number, removed: number, lines?: ToolLine[] }} FileChange
 */

/**
 * Default accessible wording of the component (the reference's
 * `defaultToolChipsLabels`). Override any key through `labels`.
 */
export const defaultToolChipsLabels = Object.freeze({
  running: 'Running',
  failed: 'Failed',
  changes: 'Files changed',
  showMoreChanges: (hiddenCount) => `Show ${hiddenCount} more`,
  diffFor: (path) => `Changes to ${path}`,
  added: (count) => `${count} added`,
  removed: (count) => `${count} removed`,
});

/** Unchanged lines kept around each change in a diff preview. */
export const DEFAULT_DIFF_CONTEXT = 2;
/** Longest diff preview, in diff lines; the rest is summarised in one line. */
export const DEFAULT_MAX_DIFF_LINES = 200;
/** Marks unchanged lines left out between two parts of a diff preview. */
export const DIFF_GAP = '⋯';

// The review surface's budget (DiffView UI_DIFF_OPTIONS): a reorder-heavy
// hunk degrades to a coarser, still exact, diff instead of blocking.
const DIFF_OPTIONS = { maxCost: 5000 };
const ROW_KIND = { context: 'context', add: 'added', del: 'removed' };

const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
const isFiniteCount = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const asText = (value) => (typeof value === 'string' ? value : '');
const textList = (value) => (Array.isArray(value) ? value.filter((item) => typeof item === 'string' && item) : []);

/**
 * Diff rows of every hunk, trimmed to `context` unchanged lines around each
 * change, plus the added/removed totals.
 */
const collectDiff = (changes, context) => {
  const groups = [];
  let added = 0;
  let removed = 0;
  for (const change of Array.isArray(changes) ? changes : []) {
    if (!change || typeof change !== 'object') continue;
    const before = asText(change.old);
    const after = asText(change.new);
    if (before === after) continue;
    const rows = toRows(diffLines(before, after, DIFF_OPTIONS));
    for (const item of toHunks(rows, context)) {
      if (item.kind !== 'hunk') continue;
      groups.push(item.rows);
      for (const row of item.rows) {
        if (row.kind === 'add') added += 1;
        else if (row.kind === 'del') removed += 1;
      }
    }
  }
  return { groups, added, removed };
};

/**
 * Lines for a diff preview plus totals; `lines` is capped at `maxLines` diff
 * lines (gap markers do not count) and ends with a "N more lines" line when
 * the cap cut it.
 */
const buildDiff = (changes, {
  context = DEFAULT_DIFF_CONTEXT,
  maxLines = DEFAULT_MAX_DIFF_LINES,
  gap = DIFF_GAP,
  moreLines = (count) => `${plural(count, 'more line')}`,
} = {}) => {
  const { groups, added, removed } = collectDiff(changes, context);
  /** @type {ToolLine[]} */
  const lines = [];
  const total = groups.reduce((sum, rows) => sum + rows.length, 0);
  let shown = 0;
  for (const [index, rows] of groups.entries()) {
    if (shown >= maxLines) break;
    if (index > 0 && gap) lines.push({ text: gap, kind: 'context' });
    for (const row of rows) {
      if (shown >= maxLines) break;
      lines.push({ text: row.text, kind: ROW_KIND[row.kind] });
      shown += 1;
    }
  }
  if (total > shown) lines.push({ text: moreLines(total - shown), kind: 'context' });
  return { lines, added, removed };
};

/**
 * Turns a file's exact-match hunks ({ old, new } pairs, as the code editor
 * returns them) into Tool Chips diff lines: every line of each hunk diffed
 * with the app's line diff (lib/diff.js), unchanged lines kept as `context`
 * (at most `context` of them around each change), changed ones as `added` /
 * `removed`. Separate parts are joined by a `⋯` context line (`gap: null`
 * leaves it out). Hunks whose old and new text are equal add nothing.
 *
 * @param {Array<{ old?: string, new?: string }>} changes
 * @param {{
 *   context?: number,
 *   maxLines?: number,
 *   gap?: string | null,
 *   moreLines?: (count: number) => string,
 * }} [options]
 * @returns {ToolLine[]}
 */
export const hunksToToolLines = (changes, options = {}) => buildDiff(changes, options ?? {}).lines;

/**
 * Counts the added and removed lines of a ToolLine list.
 *
 * @param {ToolLine[]} lines
 * @returns {{ added: number, removed: number }}
 */
export const countToolLines = (lines) => {
  let added = 0;
  let removed = 0;
  for (const line of Array.isArray(lines) ? lines : []) {
    if (line?.kind === 'added') added += 1;
    else if (line?.kind === 'removed') removed += 1;
  }
  return { added, removed };
};

/**
 * One changed file of a proposal (`{ file_path, action, additions,
 * deletions, changes, before, after }`) as a Tool Chips FileChange. Diff
 * lines come from the hunks; a created file (or one with only before/after
 * text) is diffed whole. Counts are the server's when present. Returns null
 * for an entry without a path.
 *
 * @param {object} file
 * @param {Parameters<typeof hunksToToolLines>[1]} [options]
 * @returns {FileChange | null}
 */
export const fileToToolChange = (file, options = {}) => {
  if (!file || typeof file !== 'object') return null;
  const path = asText(file.file_path) || asText(file.path);
  if (!path) return null;
  let diff = null;
  if (Array.isArray(file.changes) && file.changes.length > 0) {
    diff = buildDiff(file.changes, options ?? {});
  } else if (typeof file.after === 'string' && (file.action === 'create' || typeof file.before === 'string')) {
    const before = file.action === 'create' ? '' : file.before;
    diff = buildDiff([{ old: before, new: file.after }], options ?? {});
  } else if (file.action === 'create' && typeof file.content === 'string') {
    diff = buildDiff([{ old: '', new: file.content }], options ?? {});
  }
  /** @type {FileChange} */
  const change = {
    path,
    added: isFiniteCount(file.additions) ? file.additions : (diff?.added ?? 0),
    removed: isFiniteCount(file.deletions) ? file.deletions : (diff?.removed ?? 0),
  };
  if (diff?.lines.length) change.lines = diff.lines;
  return change;
};

/**
 * The changed files of an edit proposal (its `files`, or the legacy
 * single-file `file_path` + `changes` fields) as FileChanges, one per path.
 *
 * @param {object | null | undefined} proposal
 * @param {Parameters<typeof hunksToToolLines>[1]} [options]
 * @returns {FileChange[]}
 */
export const proposalToToolChanges = (proposal, options = {}) => {
  if (!proposal || typeof proposal !== 'object') return [];
  const files = Array.isArray(proposal.files) && proposal.files.length > 0
    ? proposal.files
    : (proposal.file_path ? [{ file_path: proposal.file_path, action: 'modify', changes: proposal.changes }] : []);
  const seen = new Set();
  const result = [];
  for (const file of files) {
    const change = fileToToolChange(file, options);
    if (!change || seen.has(change.path)) continue;
    seen.add(change.path);
    result.push(change);
  }
  return result;
};

// ------------------------------------------------------------- tool calls

/** Visible names of the code editor's tools (backend/agent/editor.py). */
export const TOOL_LABELS = Object.freeze({
  read_github_issue: 'Read issue',
  plan_change: 'Plan change',
  trace_symbols: 'Trace symbols',
  rename_codemod: 'Apply rename',
  validate_patch: 'Validate patch',
  read_file: 'Read file',
  read_files: 'Read files',
  check_scope: 'Check scope',
  read_conventions: 'Read conventions',
  check_syntax: 'Check syntax',
  check_style: 'Check style',
  check_imports: 'Check imports',
  check_completeness: 'Check related code',
  review_patch: 'Self-review',
});

/**
 * What failed, for the run's summary line ("Syntax check failed · 9
 * steps"); other tools use their label.
 */
export const FAILURE_LABELS = Object.freeze({
  validate_patch: 'Validation',
  check_scope: 'Scope check',
  read_conventions: 'Reading conventions',
  check_syntax: 'Syntax check',
  check_style: 'Style check',
  check_imports: 'Import check',
  check_completeness: 'Related code check',
  review_patch: 'Self-review',
});

/**
 * A stored call's outcome (`status`; missing means ok). Only `fail` turns
 * the chip into an error; `warn` and `skipped` stay done and are marked by
 * `outcome` so the view can show a quieter sign.
 */
const CALL_STATUSES = new Set(['ok', 'warn', 'fail', 'skipped']);

/** "read_files" -> "Read files". */
export const humanizeToolName = (tool) => {
  const words = asText(tool).replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : 'Tool';
};

/** A path list as one pill: the path itself for one file, else "N files". */
const filesDetail = (paths) => {
  if (paths.length === 1) return { detail: paths[0], detailIsCode: true };
  if (paths.length > 1) return { detail: plural(paths.length, 'file'), detailIsCode: false };
  return {};
};

const proseLines = (texts) => texts.filter(Boolean).map((text) => ({ text, kind: 'context' }));

/** First string (or string list) in a tool input, for unknown tools. */
const firstInputValue = (input) => {
  if (!input || typeof input !== 'object') return '';
  for (const value of Object.values(input)) {
    if (typeof value === 'string' && value) return value;
    const list = textList(value);
    if (list.length) return list.join(', ');
  }
  return '';
};

const describeCall = (tool, input, summary) => {
  switch (tool) {
    case 'read_github_issue':
      return { detail: asText(input.issue) || undefined, detailIsCode: true, output: proseLines([summary]) };
    case 'plan_change': {
      const files = textList(input.files);
      const created = textList(input.new_files);
      return {
        ...filesDetail([...files, ...created]),
        output: proseLines([summary, ...files, ...created.map((path) => `${path} (new)`)]),
      };
    }
    case 'trace_symbols': {
      const symbols = textList(input.symbols);
      return { detail: symbols.join(', ') || undefined, detailIsCode: true, output: proseLines([summary]) };
    }
    case 'rename_codemod':
      return { detail: asText(input.file) || undefined, detailIsCode: true, output: proseLines([summary]) };
    case 'validate_patch':
    case 'check_syntax':
    case 'check_style':
    case 'check_imports':
    case 'check_completeness':
      // "3 file(s), +15 −15, every hunk matched exactly once; not changed: a, b"
      // "readme.md:264 still says `limit`: `2`; test/normalize.ts imports a changed module"
      return { ...filesDetail(textList(input.files)), output: proseLines(summary.split(/;\s+/)) };
    case 'check_scope': {
      // {files: 13, limit: 8, mechanical: 5, symbols: {fooHelper: 12}, parts?: [{title, files, prompt}]}
      const count = isFiniteCount(input.files) ? input.files : null;
      const limit = isFiniteCount(input.limit) ? input.limit : null;
      const symbols = input.symbols && typeof input.symbols === 'object' && !Array.isArray(input.symbols)
        ? Object.entries(input.symbols).filter(([name, uses]) => name && isFiniteCount(uses))
        : [];
      const parts = (Array.isArray(input.parts) ? input.parts : []).filter((part) => part && typeof part === 'object');
      return {
        detail: count == null ? undefined : `${plural(count, 'file')}${limit == null ? '' : ` · limit ${limit}`}`,
        output: proseLines([
          summary,
          ...symbols.map(([name, uses]) => `${name}: used in ${plural(uses, 'file')}`),
          ...parts.map((part, index) => {
            const files = textList(part.files);
            const title = asText(part.title).trim() || `Part ${index + 1}`;
            return `${index + 1}. ${title}${files.length ? ` (${plural(files.length, 'file')})` : ''}`;
          }),
        ]),
      };
    }
    case 'read_conventions': {
      const sources = textList(input.sources);
      return { ...filesDetail(sources), output: proseLines([summary, ...(sources.length > 1 ? sources : [])]) };
    }
    case 'review_patch': {
      const issues = isFiniteCount(input.issues) ? input.issues : null;
      const fixed = isFiniteCount(input.fixed) ? input.fixed : null;
      const detail = issues ? `${fixed ?? 0} of ${plural(issues, 'issue')} fixed` : filesDetail(textList(input.files)).detail;
      return { detail, output: proseLines(summary.split(/;\s+/)) };
    }
    default: {
      const detail = firstInputValue(input);
      return { detail: detail || undefined, detailIsCode: Boolean(detail) && !/\s/.test(detail), output: proseLines([summary]) };
    }
  }
};

/**
 * The backend's `tool_calls` (`{ tool, input, result_summary, status? }`)
 * as Tool Chips calls, in order. The `code_edit` entry (a pointer to the
 * proposal) and malformed entries are skipped. Every stored call is
 * finished: `status: 'fail'` becomes an `error` chip, anything else `done`
 * (`warn` / `skipped` also set `outcome`, and their summary is the first
 * output line); a summary becomes the call's expandable output.
 *
 * @param {Array<{ tool?: string, input?: object, result_summary?: string, status?: string }>} toolCalls
 * @returns {ToolCall[]}
 */
export const toolCallsToCalls = (toolCalls) => {
  const result = [];
  (Array.isArray(toolCalls) ? toolCalls : []).forEach((call, index) => {
    if (!call || typeof call !== 'object' || typeof call.tool !== 'string' || !call.tool) return;
    if (call.tool === 'code_edit') return;
    const input = call.input && typeof call.input === 'object' ? call.input : {};
    const summary = asText(call.result_summary).trim();
    const outcome = CALL_STATUSES.has(call.status) ? call.status : 'ok';
    const { detail, detailIsCode, output } = describeCall(call.tool, input, summary);
    /** @type {ToolCall} */
    const mapped = {
      id: `${index}:${call.tool}`,
      label: TOOL_LABELS[call.tool] || humanizeToolName(call.tool),
      status: outcome === 'fail' ? 'error' : 'done',
    };
    if (outcome === 'warn' || outcome === 'skipped') mapped.outcome = outcome;
    if (detail) {
      mapped.detail = detail;
      if (detailIsCode) mapped.detailIsCode = true;
    }
    if (output?.length) mapped.output = output;
    result.push(mapped);
  });
  return result;
};

/** Running labels for the code editor's progress stages. */
export const STAGE_LABELS = Object.freeze({
  retrieving: 'Finding the code to change',
  planning: 'Planning the change',
  tracing: 'Tracing usages',
  reading: 'Reading files',
  editing: 'Writing the patch',
  validating: 'Validating the patch',
  repairing: 'Repairing hunks',
  checking: 'Checking syntax, style and imports',
  reviewing: 'Reviewing the change against the request',
  generating: 'Writing the answer',
});

/**
 * The live progress stage of a streaming answer (`{ stage, detail }`) as one
 * running call, or null when there is none. The server's detail ("Reading 5
 * current files from GitHub") is the label when it has one.
 *
 * @param {{ stage?: string, detail?: string } | null | undefined} stage
 * @returns {ToolCall | null}
 */
export const stageToToolCall = (stage) => {
  if (!stage || typeof stage !== 'object' || (!stage.stage && !stage.detail)) return null;
  const name = asText(stage.stage);
  const label = asText(stage.detail).trim() || STAGE_LABELS[name] || humanizeToolName(name);
  return { id: `stage:${name || 'working'}`, label, status: 'running' };
};

/**
 * A failed edit run (no proposal; the server explains why) as the failing
 * validation call, so the run's list ends where it stopped.
 *
 * @param {string} message why no patch was produced
 * @returns {ToolCall}
 */
export const failureToToolCall = (message) => {
  // The server's markdown ("## I couldn't produce a safe patch\n\n…"), one
  // line per paragraph, heading markers and inline-code backticks dropped
  // (the lines render as plain text).
  const output = proseLines(splitLines(asText(message).trim()).map((line) => (
    line.replace(/^#{1,6}\s+/, '').replace(/`([^`\n]+)`/g, '$1').trim()
  )));
  /** @type {ToolCall} */
  const call = { id: 'failure:validate_patch', label: TOOL_LABELS.validate_patch, status: 'error' };
  if (output.length) call.output = output;
  return call;
};

/**
 * The one-line summary over a run: "Working · 3 steps" while a call runs,
 * "Validation failed · 3 steps" (or "<label> failed") after an error, else
 * "4 steps · 7 files changed" (either part alone when the other is empty).
 *
 * @param {{ calls?: ToolCall[], changes?: FileChange[] }} run
 * @returns {string}
 */
export const summarizeToolRun = ({ calls = [], changes = [] } = {}) => {
  const list = Array.isArray(calls) ? calls.filter(Boolean) : [];
  const fileCount = Array.isArray(changes) ? changes.length : 0;
  const steps = list.length;
  const stepsPart = steps > 1 ? ` · ${plural(steps, 'step')}` : '';
  if (list.some((call) => call.status === 'running')) {
    return `Working${steps ? ` · ${plural(steps, 'step')}` : ''}`;
  }
  const failed = list.find((call) => call.status === 'error');
  if (failed) {
    // ids are "<index>:<tool>" (stored calls) or "failure:<tool>".
    const tool = asText(failed.id).split(':').slice(1).join(':');
    const what = FAILURE_LABELS[tool] || (/validat/i.test(`${failed.id} ${failed.label}`) ? 'Validation' : failed.label);
    return `${what} failed${stepsPart}`;
  }
  const parts = [];
  if (steps) parts.push(plural(steps, 'step'));
  if (fileCount) parts.push(`${plural(fileCount, 'file')} changed`);
  return parts.join(' · ') || 'No steps';
};

/**
 * Everything Tool Chips needs for one answer: the stored `toolCalls`, the
 * edit `proposal` (its changed files), the live `stage` while streaming, and
 * the `failure` text of a run that produced no patch.
 *
 * @param {{
 *   toolCalls?: Array<object>,
 *   proposal?: object | null,
 *   stage?: { stage?: string, detail?: string } | null,
 *   failure?: string | null,
 *   diff?: Parameters<typeof hunksToToolLines>[1],
 * }} source
 * @returns {{ summary: string, calls: ToolCall[], changes: FileChange[] }}
 */
export const buildToolChips = ({ toolCalls, proposal, stage, failure, diff } = {}) => {
  const calls = toolCallsToCalls(toolCalls);
  const running = stageToToolCall(stage);
  if (running) calls.push(running);
  // A run stopped by a failed step (e.g. a scope check that split the
  // change) already ends on that step; the answer text explains the rest.
  else if (failure && !calls.some((call) => call.status === 'error')) calls.push(failureToToolCall(failure));
  const changes = proposalToToolChanges(proposal, diff);
  return { summary: summarizeToolRun({ calls, changes }), calls, changes };
};
