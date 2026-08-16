/* Family Calendar — relay phone app, aligned 1:1 with frontend/mobile.html
   (same tabs: Events, Meals, Recipes, Browse, Shopping) over the relay
   transport: reads the NAS-published mirror (shopping list, events, meals,
   recipe library) and queues writes into the relay inbox, which the NAS
   drains on its outbound poll (adds/deletes appear at home within ~1-2 min).
   Live-only features (AI meal planning, photo scanning) stay in mobile.html.
   No inline handlers (CSP: script-src 'self'). */

const SHOP_CATS = ["Produce","Bakery","Meat & Fish","Dairy & Eggs","Pantry","Spices & Seasoning","International","Frozen","Drinks","Household","Other"];
const ICONS = [
  {key:'meeting',emoji:'💬'},{key:'doctor',emoji:'🏥'},{key:'school',emoji:'🎒'},
  {key:'football',emoji:'⚽'},{key:'swim',emoji:'🏊'},{key:'gym',emoji:'🏋'},
  {key:'yoga',emoji:'🧘'},{key:'travel',emoji:'✈️'},{key:'bbq',emoji:'🍖'},
  {key:'date',emoji:'❤️'},{key:'music',emoji:'🎵'},{key:'star',emoji:'⭐'},
  {key:'run',emoji:'🏃'},{key:'cake',emoji:'🎂'},{key:'trash',emoji:'🗑'},
  {key:'beachvolley',emoji:'🏐'},{key:'pizza',emoji:'🍕'},{key:'beer',emoji:'🍺'},
  {key:'whisky',emoji:'🥃'},{key:'coffee',emoji:'☕'},{key:'cocktail',emoji:'🍹'},
  {key:'car',emoji:'🚗'},{key:'broom',emoji:'🧹'},
];
const ICON_EMOJI = Object.fromEntries(ICONS.map(i => [i.key, i.emoji]));
const MDAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const BR_COURSES = ['main','side','soup','salad','dessert','breakfast','baking','snack','drink','other'];
const BR_COURSE_LABELS = {main:'Main course',side:'Side dish',soup:'Soup',salad:'Salad',dessert:'Dessert',
  breakfast:'Breakfast',baking:'Baking & bread',snack:'Snack',drink:'Drink',other:'Other'};
const LS_TOKEN='relay_token', LS_LIST='relay_list', LS_CAL='relay_cal', LS_WTOKEN='relay_wtoken',
      LS_RECIPES='relay_recipes', LS_CFG='relay_cfg';

let token='', wtoken='', data=null, cal=null, recipes=null, cfg=null;
let tab='events', dirty=false, saveTimer=null;
let evPerson=null, evIcon='meeting', mealWeek='this', mealWhich='today';
let evShowLater=false;                         // "later than 3 months" section expanded?
let homeLoaded=false;                          // AI tab iframe state
let shopSelDays=new Set(), shopSelWeek=null;   // F1 shopping view state
// F9 manual extras: the NAS is the source of truth, but a queued add/remove
// takes ~a poll cycle to round-trip. srvExtras is the last published set;
// pendingAdd/Del keep optimistic edits visible until the server reflects them.
let srvExtras=[], extrasPendAdd=new Map(), extrasPendDel=new Set();

const $ = id => document.getElementById(id);
function norm(s){ return (s||'').trim().toLowerCase(); }
function esc(s){ return (s||'').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toast(m){ const t=$('toast'); t.textContent=m; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),1800); }
function setDot(state){ $('dot').className='dot'+(state==='off'?' off':state==='err'?' err':''); }
function todayISO(){ const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
// Year included on purpose: renewal reminders (passport, licence) sit years out,
// and a bare "Fri 24 Dec" among them reads as if the list were unsorted.
function fmtDate(s){ const d=new Date(s+'T00:00'); return isNaN(d)?s:d.toLocaleDateString(undefined,{weekday:'short',day:'numeric',month:'short',year:'numeric'}); }

// ── Tokens ─────────────────────────────────────────────────────
function readTokenFromHash(){
  const h = location.hash || '';
  const mt = h.match(/(?:^#|[#&])t=([^&]+)/);
  const mw = h.match(/(?:^#|[#&])w=([^&]+)/);       // optional write code for trusted phones
  if (mt) localStorage.setItem(LS_TOKEN, decodeURIComponent(mt[1]));
  if (mw) localStorage.setItem(LS_WTOKEN, decodeURIComponent(mw[1]));
  if (mt || mw) history.replaceState(null,'',location.pathname + location.search);  // strip codes from URL
}
function saveToken(){
  const v = $('token-in').value.trim();
  if (!v) return;
  localStorage.setItem(LS_TOKEN, v); token = v;
  $('auth').style.display='none';
  init();
}
function saveWToken(){
  const v = $('wtoken-in').value.trim();
  if (!v) return;
  localStorage.setItem(LS_WTOKEN, v); wtoken = v;
  renderAddGate();
}
function renderAddGate(){
  $('wgate').style.display = wtoken ? 'none' : 'block';
  $('add-card').style.display = wtoken ? 'block' : 'none';
}
function onUnauthorized(){
  localStorage.removeItem(LS_TOKEN); token=''; setDot('err');
  $('app').style.display='none';
  $('auth').style.display='block';
  toast('Access code rejected');
}

// ── Data: shopping list ────────────────────────────────────────
function cacheLoad(){
  try { const d = JSON.parse(localStorage.getItem(LS_LIST));    if (d) adopt(d); } catch(e){}
  try { const c = JSON.parse(localStorage.getItem(LS_CAL));     if (c) cal = c; } catch(e){}
  try { const r = JSON.parse(localStorage.getItem(LS_RECIPES)); if (r) recipes = r; } catch(e){}
  try { const f = JSON.parse(localStorage.getItem(LS_CFG));     if (f) cfg = f; } catch(e){}
}
function adopt(doc){
  data = { week: doc.week||'', start: doc.start||'', days: doc.days||[],
           bought: new Set((doc.bought||[]).map(norm)), extras: [] };
  absorbExtras(doc);
}
// Merge the server's extras with still-pending optimistic edits (F9).
function computeExtras(){
  const map = new Map();
  srvExtras.forEach(e => { if (e && e.item) map.set(norm(e.item), e); });
  extrasPendDel.forEach(n => map.delete(n));
  extrasPendAdd.forEach((v,n) => { if (!map.has(n)) map.set(n, v); });
  return [...map.values()];
}
function absorbExtras(doc){
  srvExtras = doc.extras || [];
  const has = new Set(srvExtras.map(e => e && norm(e.item)));
  extrasPendAdd.forEach((v,n) => { if (has.has(n)) extrasPendAdd.delete(n); });   // server caught up
  extrasPendDel.forEach(n => { if (!has.has(n)) extrasPendDel.delete(n); });
  if (data) data.extras = computeExtras();
}
async function fetchList(){
  try {
    const r = await fetch('/list', { headers: { Authorization: 'Bearer ' + token } });
    if (r.status === 401) { onUnauthorized(); return; }
    if (!r.ok) throw new Error(r.status);
    const doc = await r.json();
    localStorage.setItem(LS_LIST, JSON.stringify(doc));
    // Don't clobber unsynced local ticks: only adopt server state if we're clean.
    if (!dirty) adopt(doc); else { data.week=doc.week; data.start=doc.start||''; data.days=doc.days||[]; absorbExtras(doc); }
    setDot('on'); renderShop();
  } catch(e){
    setDot('off');   // offline — keep whatever we have from cache
  }
}
function pushState(){
  clearTimeout(saveTimer);
  dirty = true; renderSub();
  saveTimer = setTimeout(async () => {
    if (!data) return;
    try {
      const r = await fetch('/state', {
        method:'POST', headers:{'Content-Type':'application/json', Authorization:'Bearer '+token},
        body: JSON.stringify({ bought:[...data.bought] }),
      });
      if (r.status === 401) return onUnauthorized();
      if (!r.ok) throw new Error(r.status);
      dirty = false; setDot('on'); renderSub();
    } catch(e){ setDot('off'); /* stay dirty; retry on reconnect */ }
  }, 500);
}
function toggle(item){
  if (!data) return;
  const k = norm(item);
  data.bought.has(k) ? data.bought.delete(k) : data.bought.add(k);
  localStorage.setItem(LS_LIST, JSON.stringify({
    week:data.week, start:data.start, days:data.days, extras:srvExtras, bought:[...data.bought] }));
  renderShop(); pushState();
}

// ── Data: calendar + recipe-library mirrors ────────────────────
async function fetchCalendar(){
  try {
    const r = await fetch('/calendar', { headers: { Authorization: 'Bearer ' + token } });
    if (r.status === 401) return onUnauthorized();
    if (!r.ok) throw new Error(r.status);
    cal = await r.json();
    localStorage.setItem(LS_CAL, JSON.stringify(cal));
    renderEvents(); renderMeals();
    // The library blob is fetched only when its signature moved.
    if ((cal.recipesSig || '') !== ((recipes && recipes.sig) || '')) fetchRecipes();
  } catch(e){ /* keep cached cal */ }
}
async function fetchRecipes(){
  try {
    const r = await fetch('/recipes', { headers: { Authorization: 'Bearer ' + token } });
    if (r.status === 401) return onUnauthorized();
    if (!r.ok) throw new Error(r.status);
    recipes = await r.json();
    localStorage.setItem(LS_RECIPES, JSON.stringify(recipes));
    if (tab === 'browse') { populateBrowseFilters(); renderBrowse(); }
  } catch(e){ /* keep cached copy */ }
}
async function fetchConfig(){
  try {
    const r = await fetch('/app-config', { headers: { Authorization: 'Bearer ' + token } });
    if (!r.ok) throw new Error(r.status);
    cfg = await r.json();
    localStorage.setItem(LS_CFG, JSON.stringify(cfg));
  } catch(e){ /* keep cached cfg */ }
}

// ── Queued writes ──────────────────────────────────────────────
async function queueCmd(type, payload, btn){
  if (!wtoken){ showTab('events'); renderAddGate(); toast('Enter the add-to-calendar code first'); return false; }
  const orig = btn ? btn.textContent : '';
  if (btn){ btn.disabled = true; btn.textContent = 'Working…'; }
  try {
    const r = await fetch('/inbox', {
      method:'POST', headers:{'Content-Type':'application/json', Authorization:'Bearer '+wtoken},
      body: JSON.stringify({ type, payload }),
    });
    if (r.status === 401) {           // write code wrong/revoked — re-prompt (keep the read token)
      localStorage.removeItem(LS_WTOKEN); wtoken=''; renderAddGate();
      toast('Add-to-calendar code rejected'); return false;
    }
    if (r.status === 429) { toast('Too many requests — wait a moment'); return false; }
    if (!r.ok) throw new Error(r.status);
    toast('Queued — appears at home shortly ✓');
    return true;
  } catch(e){ toast('Couldn’t reach server — try again'); return false; }
  finally { if (btn){ btn.disabled = false; btn.textContent = orig; } }
}

// ── Events tab ─────────────────────────────────────────────────
function renderEvents(){
  const members = (cal && cal.members) || [];
  if (evPerson === null && members.length) evPerson = members[0].id;
  $('ev-people').innerHTML = members.map(m =>
    `<button type="button" class="person-btn" data-id="${esc(m.id)}"
       style="${m.id===evPerson?`background:${esc(m.bg||'#888')};color:${esc(m.text||'#fff')};border-color:${esc(m.bg||'#888')}`:''}"
     >${esc(m.label||m.id)}</button>`).join('') || '<span class="empty">No members yet — connect at home first.</span>';
  $('ev-icons').innerHTML = ICONS.map(ic =>
    `<div class="icon-btn${ic.key===evIcon?' selected':''}" data-icon="${ic.key}">${ic.emoji}</div>`).join('');
  renderUpcoming();
}
function memberColor(id){
  const m = (cal && cal.members || []).find(x => x.id === id);
  return m ? (m.bg || '#888') : '#888';
}
// The date 3 months out, as an ISO string so it compares directly against e.date.
// setMonth overflows the way we want here (31 Dec + 3 → 31 Mar; 30 Nov + 3 → 2 Mar),
// since the cutoff only has to be about right.
function threeMonthsOut(){
  const d = new Date(); d.setHours(0,0,0,0); d.setMonth(d.getMonth() + 3);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function eventRow(e, i){
  const meta = fmtDate(e.date) + (e.endDate ? ` → ${fmtDate(e.endDate)}` : '') + (e.time ? ` · ${esc(e.time)}` : '');
  return `<div class="event-item"><span class="event-dot" style="background:${esc(memberColor(e.who))}"></span>
    <div class="event-info"><div class="event-name">${ICON_EMOJI[e.icon]||''} ${esc(e.label||'')}</div>
    <div class="event-meta">${meta}</div></div>
    <button class="event-del" data-i="${i}">✕</button></div>`;
}

function renderUpcoming(){
  const evs = (cal && cal.events) || [];
  if (!evs.length){ $('upcoming').innerHTML = '<div class="empty">Nothing coming up.</div>'; return; }

  // Carry each event's index in cal.events through the split — deleteItem() looks
  // the event up by position, so a row's data-i must survive the regrouping.
  const cut   = threeMonthsOut();
  const rows  = evs.map((e, i) => ({e, i}));
  const soon  = rows.filter(r => (r.e.date || '') < cut);
  const later = rows.filter(r => (r.e.date || '') >= cut);

  let html = soon.length
    ? soon.map(r => eventRow(r.e, r.i)).join('')
    : '<div class="empty">Nothing in the next 3 months.</div>';

  if (later.length){
    html += `<button type="button" class="later-toggle" id="later-toggle">
      <span>${evShowLater ? 'Hide' : 'Show'} ${later.length} later</span>
      <span class="later-caret">${evShowLater ? '▾' : '▸'}</span></button>`;
    if (evShowLater) html += `<div class="event-list">${later.map(r => eventRow(r.e, r.i)).join('')}</div>`;
  }
  $('upcoming').innerHTML = html;
}
async function submitAdd(){
  const label = $('ev-label').value.trim();
  const dateV = $('ev-date').value;
  const timeV = $('ev-time').value;
  if (!label) return toast('Add a name');
  if (!dateV) return toast('Pick a date');
  if (!evPerson) return toast('Pick a person');
  const p = { date: dateV, who: evPerson, icon: evIcon, label };
  if (timeV) p.time = timeV;
  const end = $('ev-end').value;
  if (end && end > dateV) p.endDate = end;
  const ok = await queueCmd('event', p, $('ev-submit'));
  if (ok) { $('ev-label').value=''; $('ev-time').value=''; $('ev-end').value=''; }
}
async function deleteItem(idx){
  const item = ((cal && cal.events) || [])[idx];
  if (!item) return;
  // Prefer the stable id; older mirrors fall back to content matching.
  const payload = item.id ? { id: item.id } : { date: item.date, who: item.who, label: item.label || '' };
  const ok = await queueCmd('event_delete', payload, null);
  if (ok) { cal.events.splice(idx, 1); localStorage.setItem(LS_CAL, JSON.stringify(cal)); renderEvents(); }
}

// ── Meals tab ──────────────────────────────────────────────────
function todayDow(){ const t=(cal&&cal.meals&&cal.meals.today)||''; if(!t) return -1; const d=new Date(t+'T00:00'); return (d.getDay()+6)%7; }
function renderMeals(){
  const m = (cal && cal.meals) || {};
  const plan = (mealWeek === 'next' ? m.plan_next : m.plan) || [];
  $('mw-this').classList.toggle('active', mealWeek==='this');
  $('mw-next').classList.toggle('active', mealWeek==='next');
  const dow = mealWeek === 'this' ? todayDow() : -1;
  $('meals-week').innerHTML = plan.length ? plan.map((d, i) => {
    const name = (d && typeof d==='object' ? d.name : '') || '—';
    return `<div class="mw-row${i===dow?' today':''}"><span class="mw-day">${MDAYS[i]||''}</span><span class="mw-name">${esc(name)}</span></div>`;
  }).join('') : `<div class="empty">${mealWeek==='next'
    ? 'Next week isn’t planned yet — create it in the home app'
    : 'No dinners planned yet'}</div>`;
  // Today/tomorrow detail only applies to the current week; hide just the
  // detail card for next week (NOT an ancestor walk — #meal-detail is the
  // card body, so parentElement chains land on the whole tab panel).
  const detail = mealWeek === 'this';
  $('meal-detail-toggle').style.display = detail ? '' : 'none';
  $('meal-detail-card').style.display = detail ? '' : 'none';
  if (detail) renderMealDetail();
}
function renderMealDetail(){
  const m = (cal && cal.meals) || {};
  $('seg-today').classList.toggle('active', mealWhich==='today');
  $('seg-tomorrow').classList.toggle('active', mealWhich==='tomorrow');
  const tm = mealWhich==='tomorrow' ? m.tomorrow_meal : m.today_meal;
  const el = $('meal-detail');
  if (!tm) { el.innerHTML = '<div class="empty">No dinner planned</div>'; return; }
  const meta = [];
  const tot = (tm.prep_time_min||0)+(tm.cook_time_min||0);
  if (tot) meta.push(`${tot} min`);
  if (tm.difficulty) meta.push(`difficulty ${tm.difficulty}/5`);
  if (tm.servings) meta.push(`serves ${tm.servings}`);
  let html = `<div class="md-title">${esc(tm.name||'')}</div>`;
  if (meta.length) html += `<div class="md-meta">${esc(meta.join('  ·  '))}</div>`;
  const ings = tm.ingredients||[], steps = tm.steps||[];
  if (ings.length){ html += `<div class="md-h">Ingredients</div>` + ings.map(g=>`<div class="md-ing">${esc(g)}</div>`).join(''); }
  if (steps.length){ html += `<div class="md-h">Method</div>` + steps.map((s,i)=>`<div class="md-step"><b>${i+1}.</b> ${esc(s)}</div>`).join(''); }
  if (!ings.length && !steps.length && tm.notes) html += `<div class="md-step">${esc(tm.notes)}</div>`;
  el.innerHTML = html;
}
// One toggle governs the whole Recipes tab — scan, upload and link import alike.
// Off means "store it in the language it was written in", which is the default the
// home server assumes when the flag is absent.
function translateOn(){ const c = $('rec-translate'); return !!(c && c.checked); }

async function importRecipe(){
  const input = $('ri-input');
  const raw = (input.value||'').trim();
  if (!raw) { toast('Paste a link or recipe text'); return; }
  const isUrl = /^https?:\/\/\S+$/i.test(raw);
  const translate = translateOn();
  const ok = await queueCmd('recipe_import', isUrl ? {url:raw, translate} : {text:raw, translate}, $('ri-submit'));
  if (ok) input.value = '';
}

// Photo scan: downscale client-side so the queued command stays small
// (~1600px JPEG ≈ 300–600 KB base64; the relay caps bodies at 1 MB), then
// queue it — the NAS AI-extracts (original language) and saves the recipe
// flagged needs_review for the home Recipe editor.

// Decode the picked file into something canvas can draw. createImageBitmap is
// tried first: it decodes a 12 MP phone photo without materialising a full-size
// <img> (Safari in a standalone PWA is quick to kill those on memory pressure),
// and applies EXIF orientation, so a sideways photo doesn't reach the AI rotated.
// The <img> path stays as the fallback.
async function decodeImage(file){
  if (window.createImageBitmap) {
    try { return await createImageBitmap(file, {imageOrientation: 'from-image'}); }
    catch (e) { /* unsupported options or codec — try the <img> path */ }
  }
  return await new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload  = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('decode failed')); };
    img.src = url;
  });
}

function encodeJpeg(src, maxDim, quality){
  const w = src.width || src.naturalWidth, h = src.height || src.naturalHeight;
  const s = Math.min(1, maxDim / Math.max(w, h));
  const c = document.createElement('canvas');
  c.width  = Math.max(1, Math.round(w * s));
  c.height = Math.max(1, Math.round(h * s));
  c.getContext('2d').drawImage(src, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', quality).split(',')[1];
}

async function scanPhoto(input){
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  const st = $('scan-status');
  st.style.display = 'block'; st.textContent = 'Preparing photo…';
  const done = msg => { st.textContent = msg; setTimeout(() => { st.style.display = 'none'; }, 8000); };

  // Each stage reports its own failure. One catch-all around the lot used to
  // blame the image for what were really encode or size problems, which left
  // nothing to act on ("try another photo" doesn't help if every photo fails).
  let src;
  try {
    src = await decodeImage(file);
  } catch (e) {
    console.error('photo decode failed', file.type, file.size, e);
    return done(`Could not read that image (${file.type || 'unknown format'}) — try another photo.`);
  }

  let b64;
  try {
    // Start big for legibility — recipe cards are dense small print — then step
    // down until it fits the relay's 1 MB cap.
    for (const [dim, q] of [[1600, 0.8], [1280, 0.7], [1024, 0.6], [800, 0.5]]) {
      b64 = encodeJpeg(src, dim, q);
      if (b64.length <= 900000) break;
    }
  } catch (e) {
    console.error('photo encode failed', file.type, file.size, e);
    return done('Could not prepare that photo — try another one.');
  } finally {
    if (src.close) src.close();               // release the ImageBitmap's memory
  }
  if (b64.length > 900000) return done('That photo is too detailed to send — try a tighter crop.');

  st.textContent = 'Queuing…';
  const ok = await queueCmd('recipe_photo', {image: b64, media: 'image/jpeg', translate: translateOn()}, null);
  done(ok ? 'Queued ✓ — AI reads it at home and saves it, flagged for review in the Recipe editor.'
          : 'Couldn’t queue — try again.');
}

// ── Shopping tab (F1: merged Have + Shop) ──────────────────────
function renderSub(){
  let label = '';
  const ds = (data && data.days || []).map(d => d.date).filter(Boolean);
  if (ds.length){                                   // rolling window: show the date range
    const fmt = s => { const d=new Date(s+'T00:00'); return isNaN(d)?s:d.toLocaleDateString(undefined,{day:'numeric',month:'short'}); };
    label = 'Next ' + ds.length + ' days · ' + fmt(ds[0]) + ' – ' + fmt(ds[ds.length-1]);
  } else if (data && data.week){
    label = 'Week ' + data.week;                    // fallback (older NAS still on ISO weeks)
  }
  $('sub').textContent = label + (dirty ? ' · saving…' : '');
}

// Sum an item's quantities across the selected days. Mirrors backend parse_qty
// families (mass→g, volume→ml, spoon→tsp, count). Keep this block in sync with
// the copy in frontend/mobile.html.
function fmtNum(n){ const r=Math.round(n*100)/100; return (Math.abs(r-Math.round(r))<1e-9)?String(Math.round(r)):String(r); }
function pickUnit(family,v){
  if (family==='mass')   return v>=1000?['kg',1000]:['g',1];
  if (family==='volume') return v>=1000?['l',1000]:(v>=100?['dl',100]:['ml',1]);
  if (family==='spoon')  return v>=48?['cup',48]:(v>=3?['tbsp',3]:['tsp',1]);
  return ['',1];
}
function renderBucket(family,lo,hi,unitWord){
  if (family==='count'){ const num=(lo===hi)?fmtNum(lo):`${fmtNum(lo)}–${fmtNum(hi)}`; return unitWord?`${num} ${unitWord}`:num; }
  const [u,div]=pickUnit(family,hi); const num=(lo===hi)?fmtNum(lo/div):`${fmtNum(lo/div)}–${fmtNum(hi/div)}`;
  return `${num} ${u}`;
}
// → Map(normName → {display, category, item})
function aggregate(days, selectedIdxs){
  const items=new Map();
  days.forEach((day,idx)=>{
    if (!selectedIdxs.has(idx)) return;
    (day.ingredients||[]).forEach(ing=>{
      const n=norm(ing.item); if (!n) return;
      let e=items.get(n);
      if (!e){ e={item:ing.item,category:ing.category,buckets:new Map(),raws:[]}; items.set(n,e); }
      if (!e.category) e.category=ing.category;
      const q=ing.qty||{};
      if (q.family && q.lo!=null){
        const key=q.family+'|'+(q.unit||'');
        let b=e.buckets.get(key);
        if (!b){ b={family:q.family,unitWord:q.unit||'',lo:0,hi:0}; e.buckets.set(key,b); }
        b.lo+=q.lo; b.hi+=q.hi;
      } else {                                     // unparseable → carry raw text
        const raw=[ing.amount,ing.unit].filter(Boolean).join(' ').trim();
        if (raw && !e.raws.includes(raw)) e.raws.push(raw);
      }
    });
  });
  const out=new Map();
  items.forEach((e,n)=>{
    const parts=[];
    e.buckets.forEach(b=>parts.push(renderBucket(b.family,b.lo,b.hi,b.unitWord)));
    e.raws.forEach(r=>parts.push(r));
    out.set(n,{display:parts.join(' + '),category:e.category||'Other',item:e.item});
  });
  return out;
}

function renderDayChips(){
  const el=$('shop-daychips');
  if (!data || !data.days.length){ el.innerHTML=''; return; }
  const allOn=data.days.every((_,i)=>shopSelDays.has(i));
  let html=`<button type="button" class="daychip${allOn?' on':''}" data-idx="all"><span class="dc-day">All</span></button>`;
  data.days.forEach((d,i)=>{
    const dom = d.date ? new Date(d.date+'T00:00').getDate() : '';   // day-of-month for the rolling view
    const lbl = (d.day||'·') + (dom ? ' '+dom : '');
    html+=`<button type="button" class="daychip${shopSelDays.has(i)?' on':''}" data-idx="${i}">
      <span class="dc-day">${esc(lbl)}</span><span class="dc-name">${esc(d.name||'')}</span></button>`;
  });
  el.innerHTML=html;
}
function shopChipClick(idxAttr){
  if (idxAttr==='all'){
    const allOn=data.days.every((_,i)=>shopSelDays.has(i));
    shopSelDays = allOn ? new Set() : new Set(data.days.map((_,i)=>i));
  } else {
    const i=parseInt(idxAttr,10);
    shopSelDays.has(i)?shopSelDays.delete(i):shopSelDays.add(i);
  }
  renderShop();
}

function renderShop(){
  if (!data) return;
  renderSub();
  // Reset the day filter whenever the actual set of days changes — the window
  // slides daily, so an index-based selection would otherwise point at the wrong day.
  const sig = data.days.map(d => d.date || d.day || '').join(',');
  if (shopSelWeek !== sig){ shopSelWeek=sig; shopSelDays=new Set(data.days.map((_,i)=>i)); }
  renderDayChips();

  // Single list, grouped by aisle in store order. One tick crosses an item off.
  const byCat = {};
  // Manual extras (F9): always shown — they're week-level, not tied to a day.
  (data.extras||[]).forEach(e => {
    if (!e || !e.item) return;
    const cat = e.category || 'Other';
    (byCat[cat]=byCat[cat]||[]).push({norm:norm(e.item), item:e.item, display:'', extra:true});
  });
  const agg = aggregate(data.days, shopSelDays);
  agg.forEach((v,n)=>{ (byCat[v.category]=byCat[v.category]||[]).push({norm:n, ...v}); });
  let html='', any=false;
  SHOP_CATS.forEach(cat => {
    const items = (byCat[cat]||[]).sort((a,b)=>a.item.localeCompare(b.item));
    if (!items.length) return;
    any = true;
    html += `<div class="shop-cat">${esc(cat)}</div>`;
    items.forEach(it => {
      const bought = data.bought.has(it.norm);
      const del = it.extra ? `<button type="button" class="ex-del" data-item="${esc(it.item)}" title="Remove">✕</button>` : '';
      html += `<label class="shop-row${bought?' bought':''}"><input type="checkbox" class="cb-bought" data-item="${esc(it.item)}" ${bought?'checked':''}/><span class="shop-item">${esc(it.item)}${it.display?` <span class="shop-qty">${esc(it.display)}</span>`:''}${it.extra?' <span class="ex-tag">extra</span>':''}</span>${del}</label>`;
    });
  });
  const buy=$('buy-view');
  const hasExtras = (data.extras||[]).length > 0;
  if (any) buy.innerHTML = html;
  else if (!data.days.length && !hasExtras) buy.innerHTML='<div class="empty">No meals planned for the days ahead</div>';
  else if (!shopSelDays.size) buy.innerHTML='<div class="empty">Select a day above to see its list</div>';
  else buy.innerHTML='<div class="empty">No ingredients for the selected days</div>';
}

// ── Manual extras (F9) ─────────────────────────────────────────
async function addExtra(){
  const inp = $('extra-input'); if (!inp) return;
  const item = (inp.value||'').trim();
  if (!item) return;
  if (!wtoken){ showTab('events'); renderAddGate(); toast('Enter the add-to-calendar code first'); return; }
  const week = data ? data.week : '';
  if (!week){ toast('No shopping week published yet'); return; }
  const n = norm(item);
  extrasPendDel.delete(n);
  extrasPendAdd.set(n, {item, who:'', category:'Other'});   // category refined once the NAS echoes it back
  data.extras = computeExtras();
  inp.value=''; renderShop();
  const ok = await queueCmd('shopping_extra', {week, item}, null);
  if (!ok){ extrasPendAdd.delete(n); data.extras = computeExtras(); renderShop(); }
}
async function removeExtra(item){
  const week = data ? data.week : ''; if (!week) return;
  const n = norm(item);
  extrasPendAdd.delete(n);
  extrasPendDel.add(n);
  data.extras = computeExtras(); renderShop();
  const ok = await queueCmd('shopping_extra', {week, item, action:'remove'}, null);
  if (!ok){ extrasPendDel.delete(n); data.extras = computeExtras(); renderShop(); }
}

// ── Browse tab (mirrors mobile.html's, fed by the mirrored library) ─
function fold(s){ return (s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/ı/g,'i').toLowerCase(); }
function brCourseOf(r){ return BR_COURSES.includes(r.course) ? r.course : 'other'; }

function populateBrowseFilters(){
  const idx = (recipes && recipes.index) || [];
  const curCourse = $('br-course').value, curCuisine = $('br-cuisine').value;
  $('br-course').innerHTML = '<option value="">All courses</option>' +
    BR_COURSES.filter(c => idx.some(r => brCourseOf(r)===c))
      .map(c => `<option value="${c}">${BR_COURSE_LABELS[c]}</option>`).join('');
  const cuis = [...new Set(idx.map(r => r.cuisine).filter(Boolean))].sort((a,b)=>a.localeCompare(b));
  $('br-cuisine').innerHTML = '<option value="">All cuisines</option>' +
    cuis.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  $('br-course').value = curCourse; $('br-cuisine').value = curCuisine;
}

function renderBrowse(){
  const idx = (recipes && recipes.index) || [];
  if (!idx.length){
    $('br-count').textContent = '';
    $('br-results').innerHTML = '<div class="empty">No recipes mirrored yet — they arrive with the next home sync.</div>';
    return;
  }
  const q = fold($('br-q').value.trim());
  const course = $('br-course').value;
  const cuisine = $('br-cuisine').value;
  const list = idx.filter(r => {
    if (course && brCourseOf(r) !== course) return false;
    if (cuisine && r.cuisine !== cuisine) return false;
    if (q){ const hay = fold([r.name, (r.tags||[]).join(' '), (r.ingredients||[]).join(' '), (r.equipment||[]).join(' ')].join(' ')); if (!hay.includes(q)) return false; }
    return true;
  });
  $('br-count').textContent = `${list.length} of ${idx.length} recipes`;
  const byC = {};
  list.forEach(r => { (byC[brCourseOf(r)] = byC[brCourseOf(r)] || []).push(r); });
  let html = '';
  BR_COURSES.forEach(c => {
    const g = byC[c];
    if (!g || !g.length) return;
    g.sort((a,b) => a.name.localeCompare(b.name));
    html += `<div class="shop-cat">${BR_COURSE_LABELS[c]} · ${g.length}</div>`;
    g.forEach(r => {
      const t = r.total_time_min || 0;
      const stars = r.rating ? '★'.repeat(r.rating) : '';
      const meta = [r.cuisine ? esc(r.cuisine) : '', t ? t+'m' : '', stars].filter(Boolean).join(' · ');
      html += `<div class="br-row" data-id="${esc(r.id)}">
        <span class="br-nm">${esc(r.name)}</span>
        <span class="br-meta">${meta}</span></div>`;
    });
  });
  $('br-results').innerHTML = html || '<div class="empty">No recipes match.</div>';
}

function openBrowseRecipe(id){
  const r = recipes && recipes.records && recipes.records[id];
  const el = $('br-detail');
  if (!r){ el.innerHTML = '<div class="empty">Recipe detail not mirrored yet.</div>'; return; }
  const meta = [];
  const t = (r.prep_time_min||0)+(r.cook_time_min||0);
  if (t) meta.push(`${t} min`);
  if (r.difficulty) meta.push(`difficulty ${r.difficulty}/5`);
  if (r.servings) meta.push(`serves ${r.servings}`);
  if (r.cuisine) meta.push(r.cuisine);
  let html = `<div class="md-title">${esc(r.name||'')}</div>`;
  if (meta.length) html += `<div class="md-meta">${esc(meta.join('  ·  '))}</div>`;
  if (r.description) html += `<div class="md-step">${esc(r.description)}</div>`;
  const ings = r.ingredients||[], steps = r.steps||[];
  if (ings.length){
    html += `<div class="md-h">Ingredients</div>` + ings.map(ig => {
      const amt = [ig.amount, ig.unit].filter(Boolean).join(' ');
      const note = ig.notes ? ` (${ig.notes})` : '';
      return `<div class="md-ing">${esc([amt, ig.item].filter(Boolean).join(' ') + note)}</div>`;
    }).join('');
  }
  if (steps.length){
    html += `<div class="md-h">Method</div>` + steps.map((s,i)=>
      `<div class="md-step"><b>${i+1}.</b> ${esc(s && s.text ? s.text : s)}</div>`).join('');
  }
  if (r.notes) html += `<div class="md-h">Notes</div><div class="md-step">${esc(r.notes)}</div>`;
  el.innerHTML = html;
  el.scrollIntoView({behavior:'smooth', block:'start'});
}

// ── AI content tab (embedded mobile.html — home network only) ──
// The AI features (meal planning, reviewed photo scan) are a live conversation
// with the home backend, so they can't ride the relay queue. On the home
// network this tab embeds the real mobile.html (instant, full-featured); away
// from home it explains itself. Requires HOME_APP_URL on the relay + an HTTPS
// reverse proxy at home (see HOME_HTTPS_SETUP.md).
function aiPanel(icon, title, text, retry){
  $('ai-status').innerHTML = `<div class="big">${icon}</div><h2>${esc(title)}</h2><p>${text}</p>` +
    (retry ? '<button id="ai-retry">Try again</button>' : '');
  const b = $('ai-retry');
  if (b) b.addEventListener('click', renderAITab);
}
async function probeHome(url){
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 2500);
  try {
    // no-cors: reachability is the only signal we need (response is opaque).
    await fetch(url + '/healthz', { mode: 'no-cors', cache: 'no-store', signal: ctl.signal });
    return true;
  } catch(e){ return false; }
  finally { clearTimeout(timer); }
}
async function renderAITab(){
  const frame = $('home-frame');
  frame.style.display = 'none';
  if (!cfg) await fetchConfig();
  // Bail out if the user left the AI tab while we were awaiting. #home-frame is
  // position:fixed at z-index 60, so showing it from a stale async continuation
  // blankets whichever tab they moved to — the data looks like it vanished. Both
  // awaits below can outlive the tab: fetchConfig is a network call and probeHome
  // waits up to 2.5s.
  if (tab !== 'ai') return;
  const homeUrl = cfg && cfg.homeUrl;
  if (!homeUrl) {
    aiPanel('⚙️', 'Not configured',
      'The home-app address isn’t set on the relay yet (HOME_APP_URL). Everything in the other tabs works from anywhere.');
    return;
  }
  aiPanel('⏳', 'Looking for home…', 'Checking whether the home network is reachable.');
  const reachable = await probeHome(homeUrl);
  if (tab !== 'ai') return;
  if (reachable) {
    if (!homeLoaded) {
      // Load it while still hidden and reveal it only once `load` fires. Revealing an
      // EMPTY iframe and letting the document stream in afterwards leaves WebKit with
      // the scroll extent it measured at that moment — nothing — and it never
      // recomputes, so the planner renders but won't scroll. (It scrolled fine on a
      // second visit, when the content was already there: that's the tell.) A hidden
      // iframe still loads its src, and `load` fires cross-origin.
      homeLoaded = true;
      aiPanel('⏳', 'Loading the planner…', 'Fetching the weekly planner from the home app.');
      const reveal = () => {
        if (tab !== 'ai' || !homeLoaded) return;   // left the tab, or home went away
        $('ai-status').innerHTML = '';
        frame.style.display = 'block';
      };
      frame.addEventListener('load', reveal, { once: true });
      setTimeout(reveal, 8000);                    // don't strand the user if load never fires
      // ?view=meals — just the AI weekly planner, chrome stripped. Embedding all of
      // mobile.html duplicated every tab this app already has, and stacked a second
      // header + tab bar inside the frame.
      frame.src = homeUrl + '/mobile.html?view=meals';
      return;
    }
    $('ai-status').innerHTML = '';
    frame.style.display = 'block';
  } else {
    homeLoaded = false; frame.src = 'about:blank';
    aiPanel('✨', 'AI content needs the home network',
      'Meal planning with AI and reviewed photo scanning run on the home server, which isn’t reachable right now. Everything in the other tabs works from anywhere.', true);
  }
}

// ── Tabs ───────────────────────────────────────────────────────
function showTab(name){
  tab = name;
  ['events','meals','recipes','browse','shop','ai'].forEach(k => {
    $('panel-'+k).classList.toggle('active', k===name);
    $('tab-'+k).classList.toggle('active', k===name);
  });
  $('home-frame').style.display = 'none';   // hidden unless the AI tab re-enables it
  const titles = {events:'Family Calendar', meals:'Meals', recipes:'Recipes', browse:'Browse', shop:'Shopping', ai:'AI content'};
  $('title').textContent = titles[name] || 'Family Calendar';
  if (name==='events'){ renderAddGate(); renderEvents(); fetchCalendar(); }
  if (name==='meals'){ renderMeals(); fetchCalendar(); }
  if (name==='browse'){ populateBrowseFilters(); renderBrowse(); if (!recipes) fetchRecipes(); }
  if (name==='shop'){ renderShop(); fetchList(); }
  if (name==='ai'){ renderAITab(); }
}

// ── Boot + wiring ──────────────────────────────────────────────
function init(){
  $('app').style.display='block';
  $('ev-date').value = todayISO();
  cacheLoad();
  if (data) renderShop();
  renderAddGate(); renderEvents(); renderMeals();
  fetchList(); fetchCalendar(); fetchConfig();
}

function wireUI(){
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  on('auth-continue', 'click', saveToken);
  on('wgate-enable',  'click', saveWToken);
  document.querySelectorAll('.tabbar-btn').forEach(b =>
    b.addEventListener('click', () => showTab(b.dataset.tab)));
  on('ev-submit', 'click', submitAdd);
  on('ri-submit', 'click', importRecipe);
  on('scan-file',   'change', e => scanPhoto(e.target));
  on('upload-file', 'change', e => scanPhoto(e.target));
  on('mw-this', 'click', () => { mealWeek='this'; renderMeals(); });
  on('mw-next', 'click', () => { mealWeek='next'; renderMeals(); });
  on('seg-today',    'click', () => { mealWhich='today'; renderMealDetail(); });
  on('seg-tomorrow', 'click', () => { mealWhich='tomorrow'; renderMealDetail(); });
  on('br-q', 'input', renderBrowse);
  on('br-course', 'change', renderBrowse);
  on('br-cuisine', 'change', renderBrowse);
  $('br-results').addEventListener('click', e => {
    const row = e.target.closest('.br-row'); if (!row) return;
    openBrowseRecipe(row.dataset.id);
  });
  $('ev-people').addEventListener('click', e => {
    const b = e.target.closest('.person-btn'); if (!b) return;
    evPerson = b.dataset.id; renderEvents();
  });
  $('ev-icons').addEventListener('click', e => {
    const b = e.target.closest('.icon-btn'); if (!b) return;
    evIcon = b.dataset.icon; renderEvents();
  });
  $('upcoming').addEventListener('click', e => {
    if (e.target.closest('.later-toggle')){ evShowLater = !evShowLater; renderUpcoming(); return; }
    const b = e.target.closest('.event-del'); if (!b) return;
    deleteItem(parseInt(b.dataset.i, 10));
  });
  $('buy-view').addEventListener('change', e => {   // one tick = bought or already at home
    if (e.target.classList.contains('cb-bought')) toggle(e.target.dataset.item);
  });
  $('buy-view').addEventListener('click', e => {     // remove a manual extra (F9)
    const b = e.target.closest('.ex-del'); if (!b) return;
    e.preventDefault();                              // don't also toggle the row's checkbox
    removeExtra(b.dataset.item);
  });
  $('shop-daychips').addEventListener('click', e => {
    const b = e.target.closest('.daychip');
    if (b) shopChipClick(b.dataset.idx);
  });
  $('extra-add').addEventListener('click', addExtra);
  $('extra-input').addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); addExtra(); } });
  window.addEventListener('online', () => { if (dirty) pushState(); fetchList(); fetchCalendar(); });
}

readTokenFromHash();
token = localStorage.getItem(LS_TOKEN) || '';
wtoken = localStorage.getItem(LS_WTOKEN) || '';
wireUI();
if (!token) { $('auth').style.display='block'; }
else init();

// Register the service worker so the app shell loads instantly / offline.
//
// The shell is served cache-first, so a deploy only reaches the phone once a new
// sw.js installs. Two things make that actually happen:
//   1. reg.update() on every launch — ask for a fresh sw.js rather than waiting for
//      the browser's own (up to 24h) update check.
//   2. reload on controllerchange — the new SW calls skipWaiting + clients.claim and
//      takes over, but THIS page was already rendered from the old cached shell, so
//      without a reload the update only appears on the *next* launch. That's why a
//      deploy used to need two relaunches (and often looked like it hadn't shipped).
// Only reload if a controller was already in charge: on a first-ever install
// controllerchange also fires, and reloading then would be pointless churn.
if ('serviceWorker' in navigator) {
  const hadController = !!navigator.serviceWorker.controller;
  let reloading = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!hadController || reloading) return;
    reloading = true;
    location.reload();
  });
  window.addEventListener('load', async () => {
    try {
      const reg = await navigator.serviceWorker.register('/sw.js');
      reg.update();
    } catch (e) { /* offline, or SW unsupported — the page still works */ }
  });
}
