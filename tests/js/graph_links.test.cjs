const { test } = require('node:test');
const assert = require('node:assert/strict');
const { encode, decode } = require('../../frontend/graph_links.js');
const sample = { v:1, origins:[{id:'A123',name:'Zoë & 李'}, {id:'W456',name:'A paper #1'}], edges:['coauthor'], workEdges:['authorship','citation'], neighborhood:'0,1' };

test('shared links round-trip Unicode people, papers, and actual connection options', () => {
  const url = new URL(encode('https://example.org/', sample));
  assert.equal(url.origin, 'https://example.org');
  assert.equal(url.search, '');
  assert.deepEqual(decode(url.hash), sample);
});

test('sharing omits credentials, institution settings, and arbitrary extra properties', () => {
  const url = encode('https://user:SECRET@example.org/?api_key=SECRET#PRIVATE', {...sample, apiKey:'SECRET', institution:'PRIVATE', elements:['hidden']});
  assert.ok(!url.includes('SECRET') && !url.includes('PRIVATE') && !url.includes('hidden'));
  assert.deepEqual(decode(new URL(url).hash), sample);
});

test('rejects malformed, oversized, duplicate, or unsafe links before any requests', () => {
  assert.equal(decode('#connections'), null);
  assert.throws(() => decode('#map=%7Bbroken'));
  assert.throws(() => decode('#map=' + 'a'.repeat(16001)));
  for (const value of [
    {...sample,v:2}, {...sample,origins:[]},
    {...sample,origins:Array(11).fill(sample.origins[0])},
    {...sample,origins:[sample.origins[0],sample.origins[0]]},
    {...sample,origins:[{id:'javascript:alert(1)',name:'Bad'}]},
    {...sample,origins:[{id:'A123',name:'x'.repeat(201)}]},
    {...sample,edges:['arbitrary']}, {...sample,workEdges:['institution']},
    {...sample,neighborhood:'1000,1000'},
  ]) assert.throws(() => encode('https://example.org/',value));
});

test('explicit empty edge groups remain empty, rather than activating defaults', () => {
  const value = {...sample,edges:[],workEdges:[]};
  assert.deepEqual(decode(new URL(encode('https://example.org/',value)).hash),value);
});
