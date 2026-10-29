const test = require('node:test');
const assert = require('node:assert/strict');
const { create, parse, MAX_NOTE } = require('../../frontend/saved_store.js');
const { encode, decode } = require('../../frontend/discovery_links.js');
const author = { id:'A1',type:'researcher',display_name:'Researcher',note:'Read this paper',saved_at:'2026-10-29T00:00:00Z' };
const raw = items => JSON.stringify({version:1,items});
function storage(initial) { let data = initial; return {getItem:()=>data,setItem:(key,value)=>{data=value;}}; }

test('saved researchers deduplicate and notes survive reload without leaking object references', () => {
  const disk = storage(), store = create(disk);
  store.add(author); store.add({...author,note:'overwrite?'});
  store.note('A1', 'New note');
  const list = store.list(); list[0].note='tampered';
  assert.equal(create(disk).list()[0].note, 'New note');
  assert.equal(store.list().length,1);
});
test('storage denial keeps an exportable in-memory list', () => {
  const store = create({getItem(){throw Error();}});
  store.add(author); assert.equal(parse(store.export())[0].note,author.note);
  assert.match(store.warning(),/visit only/);
});
test('corrupt storage is preserved and can be recovered', () => {
  let writes=0; const original='{invalid';
  const store=create({getItem:()=>original,setItem:()=>writes++});
  store.add(author); assert.equal(writes,0); assert.equal(store.recovery(),original);
  assert.equal(store.list().length,1);
});
test('quota exhaustion retains the latest note for export', () => {
  const store=create({getItem:()=>raw([author]),setItem(){throw Error('quota');}});
  store.note('A1','Keep me'); assert.equal(parse(store.export())[0].note,'Keep me');
  assert.match(store.warning(),/full or unavailable/);
});
test('invalid or excessive imports are atomic and do not erase stored data', () => {
  const store=create(storage(raw([author]))), before=store.export();
  for (const invalid of ['nope',raw([{...author,id:'javascript:evil'}]),raw([author,author]),JSON.stringify({version:2,items:[]}),raw([{...author,note:'a'.repeat(MAX_NOTE+1)}])]) {
    assert.throws(()=>store.import(invalid)); assert.equal(store.export(),before);
  }
});
test('conflicts are previewed and keep both by default; explicit replacement is possible', () => {
  const store=create(storage(raw([author]))), incoming=raw([{...author,note:'Imported'}]);
  assert.deepEqual(store.preview(incoming).conflicts,['Researcher']);
  assert.equal(store.list()[0].note,author.note);
  store.import(incoming,'both'); assert.match(store.list()[0].note,/Read this paper[\s\S]*Imported/);
  store.import(incoming,'imported'); assert.equal(store.list()[0].note,'Imported');
  store.note('A1','Local'); store.import(incoming,'current'); assert.equal(store.list()[0].note,'Local');
});
test('overlong combined notes reject the whole merge', () => {
  const store=create(storage(raw([{...author,note:'a'.repeat(MAX_NOTE)}])));
  const before=store.export(); assert.throws(()=>store.import(raw([{...author,note:'b'}]),'both')); assert.equal(store.export(),before);
});
test('removal can be undone without losing notes or context', () => {
  const store=create(storage()); store.add({...author,context:{institution:{id:'I1'},references:[{id:'T1'}]}});
  const removed=store.remove('A1'); assert.equal(store.has('A1'),false); store.add(removed);
  assert.equal(store.list()[0].note,author.note); assert.equal(store.list()[0].context.institution.id,'I1');
});
test('export and public links whitelist fields and never include notes or credentials', () => {
  const query={institution:{id:'I123',display_name:'University'},references:[{id:'W12',display_name:'Paper',secret:'key'}],note:'secret'};
  const store=create(storage()); store.add({...author,context:query,api_key:'secret'});
  assert.doesNotMatch(store.export(),/api_key|display_name.*University|secret/);
  const link=encode(query); assert.equal(link,'/discover?institution=I123&ref=W12');
  assert.equal(decode(link.split('?')[1]).references[0].id,'W12');
});
test('malformed links are rejected before network access', () => {
  for (const query of ['institution=I1','institution=A1&ref=T1','institution=I1&institution=I2&ref=T1','institution=I1&ref=evil','institution=I1&ref=T1&note=private','institution=I1&'+Array.from({length:9},(_,i)=>'ref=T'+i).join('&')]) assert.throws(()=>decode(query));
  assert.equal(decode(''),null);
});
