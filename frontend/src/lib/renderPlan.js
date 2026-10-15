/**
 * Decide which diff items DiffView renders. Rows are budgeted inside a hunk
 * as well as between items, so one huge hunk cannot render thousands of rows
 * before "show all", and `truncated` is true only when rows are really hidden.
 *
 * @param {Array<{kind: 'hunk'|'collapsed', rows?: object[], count?: number}>} items from toHunks
 * @param {{ expanded?: Set<number>, showAll?: boolean, limit?: number }} options
 * @returns {{ entries: Array<{ index: number, item: object, collapsed: boolean }>, truncated: boolean }}
 */
export const planDiffRender = (items, { expanded = new Set(), showAll = false, limit = 4000 } = {}) => {
  const entries = [];
  let rendered = 0;
  let truncated = false;
  for (let index = 0; index < items.length; index += 1) {
    const source = items[index];
    const collapsed = source.kind === 'collapsed' && !expanded.has(index);
    if (!showAll && rendered >= limit) {
      for (let rest = index; rest < items.length && !truncated; rest += 1) {
        if (items[rest].kind === 'hunk' || expanded.has(rest)) truncated = true;
      }
      // Keep trailing expanders visible; they hold no rows.
      if (collapsed) {
        entries.push({ index, item: source, collapsed: true });
        continue;
      }
      if (truncated) break;
      continue;
    }
    if (collapsed) {
      entries.push({ index, item: source, collapsed: true });
      continue;
    }
    const rows = source.rows || [];
    const budget = showAll ? Infinity : limit - rendered;
    const item = rows.length > budget ? { ...source, rows: rows.slice(0, budget) } : source;
    if (item !== source) truncated = true;
    entries.push({ index, item, collapsed: false });
    rendered += item.rows.length;
  }
  return { entries, truncated };
};
