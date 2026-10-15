import assert from 'node:assert/strict';
import test from 'node:test';
import { diffLines, toHunks, toRows } from './diff.js';
import { planDiffRender } from './renderPlan.js';

const rowsOf = (count) => Array.from({ length: count }, (_, index) => ({ kind: 'add', text: String(index), newNo: index + 1 }));

test('one huge hunk is cut at the row limit and reports truncation (FE-6)', () => {
  const before = Array.from({ length: 20000 }, (_, index) => `line ${index}`).join('\n');
  const after = before.split('\n').filter((_, index) => index % 3).join('\n');
  const items = toHunks(toRows(diffLines(before, after)), 3);
  const plan = planDiffRender(items, { limit: 4000 });
  const renderedRows = plan.entries.reduce((sum, entry) => sum + (entry.collapsed ? 0 : entry.item.rows.length), 0);
  assert.ok(renderedRows <= 4000, `rendered ${renderedRows}`);
  assert.equal(plan.truncated, true);
  const all = planDiffRender(items, { limit: 4000, showAll: true });
  assert.equal(all.truncated, false);
  assert.ok(all.entries.reduce((sum, entry) => sum + (entry.collapsed ? 0 : entry.item.rows.length), 0) > 4000);
});

test('"show all" is offered only when rows are hidden (FE-6)', () => {
  const items = [{ kind: 'collapsed', count: 100, rows: rowsOf(100) }, { kind: 'hunk', header: '@@', rows: rowsOf(3999) }];
  const plan = planDiffRender(items, { limit: 4000 });
  assert.equal(plan.truncated, false);
  assert.equal(plan.entries.length, 2);
  const exact = planDiffRender([{ kind: 'hunk', rows: rowsOf(4000) }, { kind: 'collapsed', count: 5, rows: rowsOf(5) }], { limit: 4000 });
  assert.equal(exact.truncated, false);
  assert.equal(exact.entries[1].collapsed, true);
  const over = planDiffRender([{ kind: 'hunk', rows: rowsOf(4000) }, { kind: 'hunk', rows: rowsOf(1) }], { limit: 4000 });
  assert.equal(over.truncated, true);
  assert.equal(over.entries.length, 1);
});
