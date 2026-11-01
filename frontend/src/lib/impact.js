// Pure helpers for the Change impact dialog: grouping and the Markdown report.
import { githubBlobUrl } from './citations.js';

export const MAX_IMPACT_DEPTH = 3;

/** "Direct", "2 levels away", ... */
export const depthLabel = (depth) => (Number(depth) <= 1 ? 'Direct' : `${depth} levels away`);

const count = (value, singular, plural = `${singular}s`) => `${Number(value).toLocaleString('en-US')} ${Number(value) === 1 ? singular : plural}`;

/** Dependents grouped by depth, nearest first: [{ depth, total, items }]. */
export const groupDependents = (result) => {
  const groups = new Map();
  for (const node of result?.dependents || []) {
    const depth = Number(node.depth) || 1;
    if (!groups.has(depth)) groups.set(depth, []);
    groups.get(depth).push(node);
  }
  const counts = result?.depth_counts || {};
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([depth, items]) => ({ depth, items, total: Math.max(items.length, Number(counts[String(depth)]) || 0) }));
};

/** Test functions grouped by file, in the server's order (references first). */
export const groupTestFunctions = (functions = []) => {
  const files = new Map();
  for (const item of functions) {
    if (!files.has(item.file_path)) files.set(item.file_path, []);
    files.get(item.file_path).push(item);
  }
  return [...files.entries()].map(([filePath, items]) => ({ filePath, items: [...items].sort((a, b) => a.line - b.line) }));
};

/** Why a test function is listed, in words. */
export const testMatchLabel = (item, symbol) => {
  if (item.kind === 'reference') {
    const uses = Number(item.references) > 1 ? ` (${item.references} times)` : '';
    return `uses ${symbol}${item.reference_line ? ` at line ${item.reference_line}` : ''}${uses}`;
  }
  const words = item.matched || symbol;
  return words === symbol ? `name contains ${symbol}` : `name contains “${words}”; does not mention ${symbol}`;
};

/** A path from this repository to show as the input's example. */
export const examplePath = (suggestions, fallback = '') => suggestions?.[0]?.file_path || fallback || 'path/to/file';

const link = (result, path, line) => {
  const url = githubBlobUrl(result?.github_url, result?.commit_sha, path, line || null, null);
  const label = line ? `${path}:${line}` : path;
  return url ? `[${label}](${url})` : `\`${label}\``;
};

/**
 * The report as Markdown, every file linked at the indexed commit (so the
 * links keep pointing at the code that was analysed).
 */
export const impactMarkdown = (result, { symbol = '' } = {}) => {
  if (!result?.target_file) return '';
  const lines = [];
  const commit = result.commit_sha ? ` @ \`${String(result.commit_sha).slice(0, 7)}\`` : '';
  lines.push(`## Change impact: \`${result.target_file}\``);
  lines.push('');
  lines.push(`${result.repository || 'Repository'}${commit}. ${result.summary || ''}`.trim());
  lines.push('');
  const groups = groupDependents(result);
  const total = groups.reduce((sum, group) => sum + group.total, 0);
  lines.push(`### Imported by (${count(total, 'file')})`);
  if (!groups.length) lines.push('', 'No indexed file imports it.');
  for (const group of groups) {
    lines.push('', `**${depthLabel(group.depth)} (${group.total.toLocaleString('en-US')})**`, '');
    for (const node of group.items) {
      const via = node.depth > 1 ? ` via \`${node.via}\`` : '';
      lines.push(`- ${link(result, node.file_path, node.line_number)}${via}${node.is_test ? ' (test)' : ''}`);
    }
    if (group.items.length < group.total) lines.push(`- …and ${count(group.total - group.items.length, 'more file')}`);
  }
  const tests = result.affected_tests || {};
  lines.push('', `### Affected tests (${count(tests.count || 0, 'file')})`, '');
  if (!tests.files?.length) lines.push('No test file imports it within this depth.');
  for (const file of tests.files || []) lines.push(`- ${link(result, file.file_path, file.line_number)}`);
  if (symbol && result.symbol) {
    lines.push('', `#### Test functions for \`${symbol}\``, '');
    if (!tests.functions?.length) lines.push(`No test in these files uses or is named after ${symbol}.`);
    for (const item of tests.functions || []) {
      lines.push(`- ${link(result, item.file_path, item.line)} \`${item.name}\`: ${testMatchLabel(item, symbol)}`);
    }
  }
  if (result.imports?.length) {
    lines.push('', `### Imports (${result.imports.length})`, '');
    for (const item of result.imports) lines.push(`- ${link(result, item.file_path)} (imported at line ${item.line_number})`);
  }
  if (result.method) lines.push('', `_${result.method}_`);
  return `${lines.join('\n')}\n`;
};
