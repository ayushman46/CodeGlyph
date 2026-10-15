/**
 * Split highlighted HTML into per-line HTML. Tokens that span lines (block
 * comments, template strings) are closed and reopened at line breaks. A final
 * newline terminates the last line rather than opening a new, empty one, so
 * the row count equals the file's line count.
 *
 * @param {string} html Highlighted (escaped) HTML of `code`.
 * @param {string} code The source text the HTML was produced from.
 * @returns {string[]}
 */
export const splitHighlightedLines = (html, code) => {
  const lines = [];
  let open = [];
  let current = '';
  const tagPattern = /<\/?span[^>]*>|\n|[^<\n]+|</g;
  let match;
  while ((match = tagPattern.exec(String(html ?? ''))) !== null) {
    const token = match[0];
    if (token === '\n') {
      current += '</span>'.repeat(open.length);
      lines.push(current);
      current = open.join('');
    } else if (token.startsWith('</span')) {
      open.pop();
      current += token;
    } else if (token.startsWith('<span')) {
      open.push(token);
      current += token;
    } else {
      current += token;
    }
  }
  lines.push(current + '</span>'.repeat(open.length));
  if (lines.length > 1 && /\n$/.test(String(code ?? ''))) lines.pop();
  return lines;
};

/** Whether a cited start line lies beyond the end of a file of `lineCount` lines. */
export const isBeyondEnd = (startLine, lineCount) => Boolean(startLine && lineCount >= 0 && startLine > lineCount);
