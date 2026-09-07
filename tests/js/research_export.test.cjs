const assert = require('node:assert/strict');
const test = require('node:test');
const { report, status } = require('../../frontend/research_export.js');

const date = new Date('2026-09-07T12:00:00Z');
const connection = {
  from_id: 'A1', from_name: 'Alice', to_id: 'A2', to_name: 'Bob',
  found: true, hops: 1, search_complete: true,
  edge_types: ['coauthor'], work_edge_types: [],
  steps: [{ from_id: 'A1', from_name: 'Alice', to_id: 'A2', to_name: 'Bob', type: 'coauthor', label: 'A shared paper' }],
};

test('exports ordered connection steps with source links and actual search options', () => {
  const output = report({ origins: [{ id: 'A1', name: 'Alice' }], paths: [connection] }, date);
  assert.match(output, /2026-09-07T12:00:00.000Z/);
  assert.match(output, /1 degree of separation/);
  assert.match(output, /1\. \[Alice\]\(https:\/\/openalex.org\/A1\) → \[Bob\]\(https:\/\/openalex.org\/A2\) — co-authored: A shared paper/);
  assert.match(output, /Researcher connections searched: coauthor/);
  assert.match(output, /Work connections searched: none/);
  assert.match(output, /bounded search/);
});

test('reports incomplete, interrupted, and no-path outcomes distinctly', () => {
  assert.equal(status({ found: false, search_complete: false }), 'Search incomplete — no path found yet');
  assert.equal(status({ found: false, search_complete: true }), 'No path found within the search limit');
  assert.equal(status({ error: 'cancelled' }), 'Search interrupted');
  const output = report({ paths: [{ ...connection, hops: 2, search_complete: false }] }, date);
  assert.match(output, /a shorter path may exist/);
  assert.doesNotMatch(report({ paths: [{ ...connection, search_complete: false }] }, date), /shorter path/);
});

test('escapes source text, rejects arbitrary link targets, and omits credentials', () => {
  const output = report({
    origins: [{ id: 'javascript:alert(1)', name: '[click](evil)\n# injected' }],
    paths: [{ ...connection, steps: [{ ...connection.steps[0], label: '<script>*unsafe*</script>' }] }],
    api_key: 'do-not-export-me', cookies: 'private',
  }, date);
  assert.ok(output.includes('\\[click\\](evil) \\# injected'));
  assert.ok(output.includes('\\<script\\>\\*unsafe\\*'));
  assert.ok(!output.includes('javascript:'));
  assert.ok(!output.includes('do-not-export-me'));
  assert.ok(!output.includes('private'));
});

test('citation wording retains direction and older saved paths omit unknown settings', () => {
  for (const [direction, phrase] of [['incoming', 'is cited by'], ['outgoing', 'cites'], ['mutual', 'citations in both directions']]) {
    const output = report({ paths: [{ ...connection, edge_types: undefined, work_edge_types: undefined, steps: [{ ...connection.steps[0], type: 'citation', direction }] }] }, date);
    assert.ok(output.includes(`— ${phrase}:`));
    assert.ok(!output.includes('connections searched:'));
  }
  assert.match(report({}, date), /No pair searches have completed/);
});
