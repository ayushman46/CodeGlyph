/*
 * A deliberately tiny TypeScript highlighter for the static code shown in the
 * landing page mockups. It emits Prism-compatible `token` class names so the
 * shared `.code-tokens` palette in index.css colours it, without pulling Prism
 * into the landing bundle.
 */
const TOKEN_SOURCE = [
  '(\\/\\/.*$)', // 1 comment
  '("(?:[^"\\\\]|\\\\.)*"|\'(?:[^\'\\\\]|\\\\.)*\'|`(?:[^`\\\\]|\\\\.)*`)', // 2 string
  '\\b(const|let|var|await|async|return|if|else|export|function|import|from|new|throw|typeof|of|for)\\b', // 3 keyword
  '\\b(true|false|null|undefined)\\b', // 4 boolean
  '\\b(\\d+(?:\\.\\d+)?)\\b', // 5 number
  '\\b([A-Z][A-Z0-9_]+)\\b', // 6 constant
  '\\b([A-Z][\\w$]*)\\b', // 7 class name
  '([A-Za-z_$][\\w$]*)(?=\\s*\\()', // 8 function call
  '([A-Za-z_$][\\w$]*)(?=\\s*:(?!:))', // 9 object property
  '([{}()[\\];,.:?=<>!+\\-*/|&]+)', // 10 punctuation
].join('|');

const TYPES = [null, 'comment', 'string', 'keyword', 'boolean', 'number', 'constant', 'class-name', 'function', 'property', 'punctuation'];

export const highlight = (line) => {
  const pattern = new RegExp(TOKEN_SOURCE, 'g');
  const parts = [];
  let cursor = 0;
  let match = pattern.exec(line);
  while (match) {
    if (match.index > cursor) parts.push(line.slice(cursor, match.index));
    const group = match.findIndex((value, index) => index > 0 && value !== undefined);
    parts.push(
      <span key={match.index} className={`token ${TYPES[group] || 'plain'}`}>
        {match[0]}
      </span>,
    );
    cursor = match.index + match[0].length;
    match = pattern.exec(line);
  }
  if (cursor < line.length) parts.push(line.slice(cursor));
  return parts;
};

export default highlight;
