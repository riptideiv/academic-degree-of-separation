const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {encode, decode} = require('../../frontend/graph_links.js');
const source = fs.readFileSync(require('node:path').join(__dirname,'../../frontend/app.js'),'utf8');
const restoreFunction = source.slice(source.indexOf('  async function restoreMap()'),source.indexOf('\n  restoreMap();'));
function harness(hash, statuses = []) {
  const calls = [], settings = [], errors = [], events = new Map();
  const context = vm.createContext({
    window:{GraphLinks:{decode},location:{hash,pathname:'/',search:'',reload:()=>calls.push('reload')},history:{replaceState:()=>calls.push('clear hash')},addEventListener:(name,handler)=>events.set(name,handler)},
    DEFAULT_SETTINGS:{},applySettings:value=>settings.push(value),loadSavedState:()=>calls.push('restore saved'),
    updateOverlays:()=>calls.push('update'),showProgress:(message,error)=>errors.push({message,error}),
    addResearcher:async author=>{calls.push(author.id);return statuses.shift() || 'done';},
  });
  vm.runInContext(restoreFunction+'\nthis.restoreMap=restoreMap;',context);
  return {...context,calls,settings,errors,events};
}
const shared = {v:1,origins:[{id:'A1',name:'First'},{id:'W2',name:'Paper'},{id:'A3',name:'Third'}],edges:['citation'],workEdges:[],neighborhood:'1,4'};
const hash = new URL(encode('https://example.org/',shared)).hash;

test('valid shared maps take priority over local graphs and rebuild people and works in order',async()=>{
  const h=harness(hash); await h.restoreMap();
  assert.deepEqual(h.calls,['clear hash','A1','W2','A3','update']);
  assert.equal(h.settings[0].edgeCoauthor,false);
  assert.equal(h.settings[0].edgeCitation,true);
  assert.equal(h.settings[0].workEdgeAuthorship,false);
  assert.equal(h.settings[0].workEdgeCitation,false);
  assert.equal(h.settings[0].neighborhood,'1,4');
});

test('a cancelled or failed shared search stops before requesting the next origin',async()=>{
  for (const status of ['cancelled','error']) {
    const h=harness(hash,['done',status]); await h.restoreMap();
    assert.deepEqual(h.calls,['clear hash','A1','W2','update']);
  }
});

test('invalid shared links retain local graphs, explain the problem, and make no graph requests',async()=>{
  const h=harness('#map=broken'); await h.restoreMap();
  assert.deepEqual(h.calls,['restore saved','update']);
  assert.equal(h.settings.length,0);
  assert.equal(h.errors[0].error,true);
});

test('ordinary anchors and empty URLs restore the local map without starting new searches',async()=>{
  for(const hash of ['', '#search-input']) {
    const h=harness(hash); await h.restoreMap();
    assert.deepEqual(h.calls,['restore saved','update']);
  }
});

test('pasting a shared link on an already open map reloads into the restore flow',()=>{
  const h=harness('#map=example'); h.events.get('hashchange')();
  assert.deepEqual(h.calls,['reload']);
  const ordinary=harness('#search-input'); ordinary.events.get('hashchange')();
  assert.deepEqual(ordinary.calls,[]);
});
