import { diffLines, diffStats } from './diff.js';

/**
 * Line-ending helpers for the review UI. diff.js deliberately compares lines
 * with CRLF folded to LF (so a CRLF file does not show every line changed),
 * which also hides changes that only touch line endings or the final newline.
 * These helpers keep those changes visible and stop the review editor (a
 * <textarea>, whose value is always LF-only) from rewriting a CRLF file.
 */

/** 'none' | 'lf' | 'crlf' | 'mixed' */
export const eolStyle = (text) => {
  const value = String(text ?? '');
  const crlf = (value.match(/\r\n/g) || []).length;
  const lf = (value.match(/\n/g) || []).length - crlf;
  if (!crlf && !lf) return 'none';
  if (crlf && lf) return 'mixed';
  return crlf ? 'crlf' : 'lf';
};

const endsWithNewline = (text) => /\n$/.test(String(text ?? ''));

/**
 * Re-apply the base file's line endings to text edited in a textarea.
 * A consistent CRLF file stays CRLF. In a mixed file every unchanged line
 * keeps its own ending and new lines take the ending of the line before them.
 * LF and single-line files are returned unchanged.
 */
export const restoreLineEndings = (edited, base) => {
  const text = String(edited ?? '');
  const style = eolStyle(base);
  if (style === 'lf' || style === 'none') return text;
  const lf = text.replace(/\r\n/g, '\n');
  if (style === 'crlf') return lf.replace(/\n/g, '\r\n');
  // Mixed: endings of the base lines, matched to edited lines by a line diff.
  const baseText = String(base ?? '');
  const baseEndings = [];
  baseText.replace(/[^\n]*\n|[^\n]+$/g, (line) => {
    baseEndings.push(line.endsWith('\r\n') ? '\r\n' : '\n');
    return line;
  });
  const fallback = baseEndings.filter((ending) => ending === '\r\n').length * 2 >= baseEndings.length ? '\r\n' : '\n';
  const endings = [];
  let oldIndex = 0;
  let previous = fallback;
  for (const op of diffLines(baseText, lf)) {
    if (op.type === 'equal') {
      op.lines.forEach(() => {
        previous = baseEndings[oldIndex] || previous;
        endings.push(previous);
        oldIndex += 1;
      });
    } else if (op.type === 'delete') {
      oldIndex += op.lines.length;
    } else {
      op.lines.forEach(() => endings.push(previous));
    }
  }
  const parts = lf.split('\n');
  return parts.map((part, index) => (index < parts.length - 1 ? `${part}${endings[index] || fallback}` : part)).join('');
};

// Visible stand-ins so a byte-level comparison can reuse the line differ: a
// CR becomes part of the line, and a last line without a newline differs
// from the same line with one, exactly as git counts them.
const markEndings = (text) => {
  const value = String(text ?? '');
  const marked = value.replace(/\r/g, '␍');
  return marked && !marked.endsWith('\n') ? `${marked}␤` : marked;
};

const needsExactComparison = (before, after) => (
  String(before ?? '').includes('\r') || String(after ?? '').includes('\r')
  || (String(before ?? '') !== '' && String(after ?? '') !== '' && endsWithNewline(before) !== endsWithNewline(after))
);

/**
 * Added/deleted line counts as git reports them: line-ending and final
 * newline changes count, unlike diffStats(diffLines(...)).
 */
export const exactStats = (before, after, options) => {
  if (!needsExactComparison(before, after)) return diffStats(diffLines(before ?? '', after ?? '', options));
  return diffStats(diffLines(markEndings(before), markEndings(after), options));
};

/**
 * Describe line-ending and final-newline differences, or null when there are
 * none. `changedLines` counts lines whose ending differs among lines whose
 * text is unchanged (by position after the common prefix/suffix, cheap).
 */
export const describeEolChange = (before, after) => {
  const old = String(before ?? '');
  const next = String(after ?? '');
  if (old === '' || next === '') return null;
  const styleBefore = eolStyle(old);
  const styleAfter = eolStyle(next);
  const finalBefore = endsWithNewline(old);
  const finalAfter = endsWithNewline(next);
  const endingsChanged = styleBefore !== styleAfter && styleBefore !== 'none' && styleAfter !== 'none';
  let changedLines = 0;
  if (endingsChanged || styleBefore === 'mixed' || styleAfter === 'mixed') {
    const oldLines = old.match(/[^\n]*\n|[^\n]+$/g) || [];
    const newLines = next.match(/[^\n]*\n|[^\n]+$/g) || [];
    const ops = diffLines(old, next);
    let oldIndex = 0;
    let newIndex = 0;
    ops.forEach((op) => {
      if (op.type === 'equal') {
        op.lines.forEach(() => {
          if (oldLines[oldIndex]?.endsWith('\r\n') !== newLines[newIndex]?.endsWith('\r\n')) changedLines += 1;
          oldIndex += 1;
          newIndex += 1;
        });
      } else if (op.type === 'delete') oldIndex += op.lines.length;
      else newIndex += op.lines.length;
    });
  }
  if (!changedLines && finalBefore === finalAfter) return null;
  return { styleBefore, styleAfter, changedLines, finalNewlineBefore: finalBefore, finalNewlineAfter: finalAfter };
};


/**
 * The indentation unit a file uses ('\t', or 2/4/... spaces), for the review
 * editor's Tab key. Looks at the change in leading spaces between lines, so
 * nested blocks and alignment do not skew it; defaults to two spaces.
 */
export const detectIndent = (text, fallback = '  ') => {
  const lines = String(text ?? '').split('\n', 4000);
  let tabLines = 0;
  let spaceLines = 0;
  const steps = new Map();
  let previous = 0;
  for (const line of lines) {
    if (!line.trim()) continue;
    if (line.startsWith('\t')) {
      tabLines += 1;
      continue;
    }
    const width = /^ */.exec(line)[0].length;
    if (width) spaceLines += 1;
    const step = Math.abs(width - previous);
    previous = width;
    if (step >= 2 && step <= 8) steps.set(step, (steps.get(step) || 0) + 1);
  }
  if (tabLines > spaceLines) return '\t';
  if (!spaceLines) return fallback;
  let best = 0;
  let bestCount = 0;
  steps.forEach((count, step) => {
    if (count > bestCount || (count === bestCount && step < best)) {
      best = step;
      bestCount = count;
    }
  });
  return best ? ' '.repeat(best) : fallback;
};
