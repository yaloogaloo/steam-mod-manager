'use strict';
const paths = {
  filter: 'M4 6h16 M7 12h10 M10 18h4',
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  dense: 'M3 3h4v4H3z M10 3h4v4h-4z M17 3h4v4h-4z M3 10h4v4H3z M10 10h4v4h-4z M17 10h4v4h-4z M3 17h4v4H3z M10 17h4v4h-4z M17 17h4v4h-4z',
  layers: 'M3 8l9-5 9 5-9 5z M3 12l9 5 9-5 M3 16l9 5 9-5',
  download: 'M12 3v12 M7 10l5 5 5-5 M4 17v4h16v-4',
  'move-top':'M4 4h16 M12 20V8 M6 14l6-6 6 6',
  'move-bottom':'M4 20h16 M12 4v12 M6 10l6 6 6-6',
  settings: 'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',
  search: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  game: 'M6 7h12l3 10-3 2-4-4h-4l-4 4-3-2z M6 10v4 M4 12h4 M16 11h1 M18 13h1',
  refresh: 'M20 7a9 9 0 1 0 1 7 M20 3v5h-5',
  close: 'M5 5l14 14 M19 5L5 19',
  image: 'M3 4h18v16H3z M3 16l5-5 4 4 3-3 6 6 M14 8h1',
  package: 'M21 8l-9-5-9 5 9 5z M3 8v9l9 5 9-5V8 M12 13v9',
  play: 'M8 5l11 7-11 7z',
  'refresh-cw': 'M20 7a9 9 0 1 0 1 7 M20 3v5h-5',
  'package-minus': 'M21 8l-9-5-9 5 9 5z M3 8v9l9 5 4-2 M12 13v9 M16 16h6 M7 5l9 5',
  pencil: 'M16 3l5 5-12 12-6 1 1-6z M14 5l5 5',
  'more-horizontal': 'M5 12h.01 M12 12h.01 M19 12h.01',
  'book-open': 'M12 5v16 M12 5C8 3 5 3 2 4v15c3-1 6-1 10 2 4-3 7-3 10-2V4c-3-1-6-1-10 1',
  'external-link': 'M14 3h7v7 M10 14L21 3 M10 3H3v18h18v-7',
  folder: 'M3 5h6l2 2h10v13H3z',
  'file-text': 'M14 2H4v20h16V8z M14 2v6h6 M8 12h8 M8 16h8',
  upload: 'M12 17V3 M7 8l5-5 5 5 M4 17v4h16v-4',
  tags: 'M3 3h7l11 11-7 7L3 10z M7 7h.01',
  'circle-alert': 'M12 8v5 M12 17h.01 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  'chevron-down': 'M6 9l6 6 6-6',
  star: 'M12 3.25l2.7 5.47 6.04.88-4.37 4.26 1.03 6.02L12 17.04l-5.4 2.84 1.03-6.02L3.26 9.6l6.04-.88z',
  check: 'M5 12l4 4 10-10',
  swap: 'M9 6L4 12l5 6 M15 6l5 6-5 6'
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.image}"/></svg>`;
document.querySelectorAll('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
const $ = id => document.getElementById(id),
  escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  } [c]));
const S = {
  snapshot:0,
  assets: [],
  rows: [],
  collections: [],
  browse: 'all',
  current: null,
  selected: new Set(),
  compact: false,
  epoch: 0,
  detailEpoch: 0,
  covers: new Map(),
  coverPending: new Set(),
  coverQueue: [],
  coverActive: 0,
  columns: 1,
  cell: 270,
  width: 240,
  details: null,
  inspectorPane: 'description',
  inspectorPaneFor: '',
  fileState: null,
  fileNoteEdit: null
};
let bridge, serial = 0;
const requests = new Map();

function rpc(operation, args = {}, writable = false) {
  return new Promise((resolve, reject) => {
    const id = String(++serial);
    requests.set(id, {
      resolve,
      reject
    });
    bridge[writable ? "command" : "request"](id, operation, JSON.stringify(args));
  });
}

function size(value) {
  if (value === null || value === undefined) return '大小未知';
  if (value === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const n = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return (value / 1024 ** n).toLocaleString('en-US', {
    maximumFractionDigits: n > 1 ? 1 : 0
  }) + ' ' + units[n];
}
const deploymentLabel = a => ({deployed:'已部署',not_deployed:'未部署',failed:'部署失败'})[a.deploy_state??(a.deployed?'deployed':'not_deployed')]||'状态无法确认';
const stateBadge = a => `<span class="badge ${a.deployed?'deployed':''}">${deploymentLabel(a)}</span>`;

function coverMarkup(a, klass = '') {
  const value = S.covers.get(a.token);
  return value && value !== 'error' ? `<img class="${klass}" src="${value}" alt="${escape(a.name)} 封面">` : `<div class="cover-placeholder ${klass}">${icon('image')}${value==='error'?'封面读取失败':S.covers.has(a.token)?'暂无可用封面':'正在读取真实封面'}</div>`;
}

function queueCover(a) {
  if (S.covers.has(a.token) || S.coverPending.has(a.token)) return;
  S.coverPending.add(a.token);
  S.coverQueue.push(a);
  pumpCovers();
}

function pumpCovers() {
  while (S.coverActive < 4 && S.coverQueue.length) {
    const a = S.coverQueue.shift();
    S.coverActive++;
    rpc('cover', {
      token: a.token
    }).then(value => {
      S.covers.set(a.token, value);
    }).catch(() => S.covers.set(a.token, 'error')).finally(() => {
      while (S.covers.size > 300) S.covers.delete(S.covers.keys().next().value);
      document.querySelectorAll(`[data-cover="${a.token}"]`).forEach(el => el.innerHTML = coverMarkup(a));
      if (S.current?.token === a.token) {
        const el = $('inspector-image');
        if (el) el.innerHTML = coverMarkup(a, 'inspector-cover');
      }
      if (typeof updateDeploymentCover === 'function') updateDeploymentCover(a);
      const detailImage = $('detail-image');
      if (detailImage?.dataset.token === a.token) detailImage.innerHTML = coverMarkup(a, 'detail-cover');
      S.coverActive--;
      S.coverPending.delete(a.token);
      pumpCovers();
    });
  }
}

function options(id, values, label) {
  const el = $(id),
    previous = el.value;
  el.replaceChildren(new Option(label, ''), ...values.map(v => new Option(v, v)));
  if (values.includes(previous)) el.value = previous;
}
function browseSignature() {
  return [$('game').value, S.browse, $('library-sort').value, $('search').value.trim().toLocaleLowerCase(), $('status').value, $('type').value, $('platform').value, $('collection').value].join('\u0000');
}
function orderCounts(rows) {
  const counts = new Map();
  for (const row of rows) if (row.internal_id) counts.set(row.internal_id, (counts.get(row.internal_id) || 0) + 1);
  return counts;
}
function rowOrderKey(asset, counts) {
  if (asset.internal_id && counts.get(asset.internal_id) === 1) return 'id:' + asset.internal_id;
  return 'token:' + asset.token;
}
function identityKey(asset) {
  return asset?.internal_id ? 'id:' + asset.internal_id : 'token:' + (asset?.token || '');
}
function anchorByIdentity() {
  const top = $('viewport').scrollTop;
  if (!S.rows.length || !S.cell) return null;
  const index = Math.min(S.rows.length - 1, Math.max(0, Math.floor(top / S.cell) * S.columns));
  const row = S.rows[index];
  return row ? {key: identityKey(row), offset: (top % S.cell) / S.cell} : null;
}
function restoreIdentityAnchor(anchor) {
  if (!anchor) return;
  const index = S.rows.findIndex(asset => identityKey(asset) === anchor.key);
  if (index < 0) return;
  const top = (Math.floor(index / S.columns) + anchor.offset) * S.cell;
  if ($('viewport').scrollTop !== top) $('viewport').scrollTop = top;
}
// Existing cards keep their session order until the user changes sort, search, filter, or game.
function orderRows(rows) {
  const signature = browseSignature();
  const rank = asset => Number(asset.sort_ranks?.[$('library-sort').value] ?? 0);
  const counts = orderCounts(rows);
  const keyOf = asset => rowOrderKey(asset, counts);
  const changed = signature !== S.browseSignature || !Array.isArray(S.rowOrder);
  if (changed) rows.sort((a, b) => rank(a) - rank(b));
  else {
    const previous = new Map(S.rowOrder.map((key, index) => [key, index]));
    const known = [], unknown = [];
    for (const row of rows) (previous.has(keyOf(row)) ? known : unknown).push(row);
    known.sort((a, b) => previous.get(keyOf(a)) - previous.get(keyOf(b)));
    unknown.sort((a, b) => rank(a) - rank(b));
    rows.splice(0, rows.length, ...known, ...unknown);
  }
  S.browseSignature = signature;
  S.rowOrder = rows.map(keyOf);
  return changed;
}
async function reloadOpenDetails(libraryEpoch) {
  const asset = S.current;
  if (!asset || $('inspector').hidden) return;
  const detailEpoch = ++S.detailEpoch;
  try {
    const detail = await rpc('detail', {token: asset.token});
    if (libraryEpoch !== S.epoch || detailEpoch !== S.detailEpoch || S.current?.token !== asset.token) return;
    S.details = detail;
    asset.size = detail.size;
    drawInspector(asset, detail);
    renderGrid();
  } catch (error) {
    if (libraryEpoch === S.epoch && detailEpoch === S.detailEpoch && $('inspector-description'))
      $('inspector-description').textContent = '详情读取失败：' + error.message;
  }
}
async function load() {
  if (typeof closeDeploymentInspector === 'function') closeDeploymentInspector(false);
  if($('inspector-type-menu').matches(':popover-open'))$('inspector-type-menu').hidePopover();
  closeFileTypeMenu(false);
  closeMoreMenu(false);
  syncGamePicker();
  const epoch = ++S.epoch;
  const previousCollection = $('collection').value;
  const previousAssets = S.assets;
  const anchor = anchorByIdentity();
  const scrollTop = $('viewport').scrollTop;
  let userScrolled = false;
  const noteScroll = () => { userScrolled = true; };
  $('viewport').addEventListener('scroll', noteScroll);
  $('refresh').disabled = true;
  $('results').hidden = false;
  $('results').textContent = '正在读取数据库快照…';
  for (const asset of S.coverQueue) S.coverPending.delete(asset.token);
  S.coverQueue = [];
  try {
    const result = await rpc('library', {
      game: $('game').value
    });
    if (epoch !== S.epoch) return false;
    S.snapshot = result.snapshot;
    S.assets = result.assets;
    S.collections = result.collections;
    const previousType=$('type').value;
    $('type').replaceChildren(new Option('全部类型',''),new Option('未设置类型','none'),...(result.types||[]).map(t=>new Option(t.name,String(t.type_id))));
    if([...$('type').options].some(o=>o.value===previousType))$('type').value=previousType;
    options('platform', [...new Set(S.assets.map(a=>a.platform))].sort(), '全部来源');
    $('collection').replaceChildren(new Option('全部合集', ''), ...S.collections.map(c => new Option(c.name, c.id)));
    if (S.collections.some(c=>c.id===previousCollection)) $('collection').value=previousCollection;
    $('all-count').textContent = S.assets.length;
    $('fav-count').textContent = S.assets.filter(a => a.favorite).length;
    $('collection-count').textContent = S.collections.length;
    $('summary').textContent = `${S.assets.length.toLocaleString()} 个 Mod · ${S.assets.filter(a=>a.deployed).length.toLocaleString()} 已部署 · ${S.assets.filter(a=>a.favorite).length} 已收藏`;
    $('game-caption').textContent = $('game').selectedOptions[0]?.text + ' · AppID ' + $('game').value;
    S.selectionIds = new Set(previousAssets.filter(asset => S.selected.has(asset.token)).map(asset => asset.internal_id));
    filter();
    if (!userScrolled && $('viewport').scrollTop === scrollTop) restoreIdentityAnchor(anchor);
    await reloadOpenDetails(epoch);
    if (epoch !== S.epoch) return false;
    if(bridge?.remember_session)bridge.remember_session($('game').value,S.space||'library');
    return true;
  } catch (error) {
    if (epoch === S.epoch) {
      S.assets = [];
      S.rows = [];
      $('grid').replaceChildren();
      $('results').hidden = false;
      $('results').textContent = '加载失败：' + error.message;
    }
    return false;
  } finally {
    $('viewport').removeEventListener('scroll', noteScroll);
    if (epoch === S.epoch) $('refresh').disabled = false;
    if (S.space==='acquire' && typeof refreshAcquisition==='function') refreshAcquisition();
    if (S.space==='deployment' && typeof refreshDeployment==='function') refreshDeployment();
  }
}

function filter() {
  const keptIds = S.selectionIds;
  S.selectionIds = null;
  if (typeof renderCollectionMode === 'function' && renderCollectionMode()) return;
  const query = $('search').value.trim().toLocaleLowerCase(),
    status = $('status').value,
    type = $('type').value,
    platform = $('platform').value,
    collection = $('collection').value;
  const members = collection ? new Set(S.collections.find(c => c.id === collection)?.tokens || []) : null;
  const anyMembers = S.browse === 'collections' && !collection ? new Set(S.collections.flatMap(c => c.tokens)) : null;
  S.rows = S.assets.filter(a => (!query || (a.search_fields || [a.name,a.workspace_id]).some(value => value.toLocaleLowerCase().includes(query))) && (S.browse !== 'favorite' || a.favorite) && (!anyMembers || anyMembers.has(a.token)) && (!members || members.has(a.token)) && (!type || (type==='none'?a.type_id==null:String(a.type_id)===type)) && (!platform || a.platform === platform) && (status === 'all' || status === 'deployed' && a.deployed || status === 'undeployed' && a.deploy_state==='not_deployed' || status === 'error' && a.status || a.filter_keys?.includes(status)));
  $('library-filters').classList.toggle('active',!!platform);
  $('library-filters').title=platform?'来源：'+platform:'来源筛选';
  const browseChanged = orderRows(S.rows);
  const visible = new Set(S.rows.map(a => a.token));
  if (keptIds) S.selected = new Set(S.rows.filter(a => keptIds.has(a.internal_id)).map(a => a.token));
  else S.selected = new Set([...S.selected].filter(t => visible.has(t)));
  if (S.current) {
    const next = S.current.internal_id ? S.rows.find(a => a.internal_id === S.current.internal_id) : null;
    if (next) S.current = next;
    else if (!visible.has(S.current.token)) closeInspector();
  }
  $('results').textContent = `${S.rows.length.toLocaleString()} 个匹配`;
  $('results').hidden = S.rows.length === S.assets.length;
  $('empty').hidden = !!S.rows.length;
  if (browseChanged) $('viewport').scrollTop = 0;
  selectionUI();
  reflow();
}

function selectionUI() {
  $('selection-tools').hidden = !S.selected.size;
  $('selected-count').textContent = `${S.selected.size} 项已勾选`;
  $('batch-edit').disabled=S.readOnly||S.busy;
  if (typeof renderCollectionTools==='function' && S.browse==='collections') renderCollectionTools();
}

function reflow(anchor = null) {
  const viewport = $('viewport');
  const width = viewport.clientWidth - 8;
  const min = S.compact ? 166 : 186;
  const gap = 12;
  S.columns = Math.max(1, Math.floor((width + gap) / (min + gap)));
  S.width = Math.floor((width - (S.columns - 1) * gap) / S.columns);
  S.coverHeight = Math.round(S.width / 1.78);
  S.cell = S.coverHeight + 61 + gap;
  $('grid').style.height = Math.ceil(S.rows.length / S.columns) * S.cell + 'px';
  if (anchor) restoreAnchor(anchor);
  else renderGrid();
}

function browseAnchor() {
  const top = $('viewport').scrollTop;
  return {
    token: S.rows[Math.floor(top / S.cell) * S.columns]?.token,
    offset: (top % S.cell) / S.cell
  };
}

function restoreAnchor(anchor) {
  const index = S.rows.findIndex(a => a.token === anchor.token);
  if (index >= 0) $('viewport').scrollTop = (Math.floor(index / S.columns) + anchor.offset) * S.cell;
  renderGrid();
}

function cardMeta(a) {
  return `<span class="platform">${escape(a.platform)}</span>${a.type_id!=null&&a.type&&a.type!=='类型未知'?`<span class="type-label" title="${escape(a.type)}">${escape(a.type)}</span>`:''}${a.deployed?'<span class="badge deployed">已部署</span>':''}${a.content_status==='content_missing'?'<span class="badge content-missing">内容缺失</span>':''}`;
}
function syncCard(el, a) {
  el.dataset.token = a.token;
  if (a.internal_id) el.dataset.internalId = a.internal_id;
  el.setAttribute('aria-label', a.name + '，' + a.platform + '，' + deploymentLabel(a));
  const cover = el.querySelector('[data-cover]');
  if (cover && cover.dataset.cover !== a.token) cover.dataset.cover = a.token;
  const title = el.querySelector('.card-title');
  if (title && title.textContent !== a.name) {
    title.textContent = a.name;
    title.title = a.name;
  }
  const meta = el.querySelector('.card-meta');
  const nextMeta = cardMeta(a);
  if (meta && meta.innerHTML !== nextMeta) meta.innerHTML = nextMeta;
  const star = el.querySelector('[data-favorite]');
  if (star) {
    const label = a.favorite ? '取消收藏' : '收藏';
    star.dataset.favorite = a.token;
    star.classList.toggle('is-favorite', !!a.favorite);
    star.title = label;
    star.setAttribute('aria-label', label);
    star.setAttribute('aria-pressed', String(!!a.favorite));
    star.disabled = !!(S.readOnly || S.busy);
  }
  const check = el.querySelector('.check');
  if (check) check.setAttribute('aria-label', '勾选 ' + a.name);
}
function renderGrid() {
  if (S.browse === 'collections' && !$('collection').value) return;
  const viewport = $('viewport'),
    first = Math.max(0, Math.floor(viewport.scrollTop / S.cell) - 1) * S.columns,
    last = Math.min(S.rows.length, (Math.ceil((viewport.scrollTop + viewport.clientHeight) / S.cell) + 1) * S.columns);
  S.renderRange = [first, last];
  const visible = new Set(S.rows.slice(first, last).map(a => a.token));
  if (S.current) visible.add(S.current.token);
  S.coverQueue = S.coverQueue.filter(a => {
    if (visible.has(a.token)) return true;
    S.coverPending.delete(a.token);
    return false;
  });
  const grid = $('grid');
  const idCounts = orderCounts(S.rows);
  const byToken = new Map();
  const byId = new Map();
  const seenIds = new Set();
  for (const el of grid.children) {
    byToken.set(el.dataset.token, el);
    const id = el.dataset.internalId;
    if (!id || idCounts.get(id) !== 1) continue;
    if (seenIds.has(id)) byId.delete(id);
    else { seenIds.add(id); byId.set(id, el); }
  }
  const focused = document.activeElement?.closest('.card');
  const used = new Set();
  let before = grid.firstElementChild;
  for (let i = first; i < last; i++) {
    const a = S.rows[i];
    let el = byToken.get(a.token) || (a.internal_id && idCounts.get(a.internal_id) === 1 ? byId.get(a.internal_id) : null);
    if (el && used.has(el)) el = null;
    if (!el) {
      el = document.createElement('article');
      el.tabIndex = 0;
      el.innerHTML = `<div class="cover"><div data-cover="${a.token}" style="width:100%;height:100%;display:grid;place-items:center">${coverMarkup(a)}</div><button class="cover-star ${a.favorite?'is-favorite':''}" data-favorite="${a.token}" title="${a.favorite?'取消收藏':'收藏'}" aria-label="${a.favorite?'取消收藏':'收藏'}" aria-pressed="${a.favorite}" ${S.readOnly||S.busy?'disabled':''}>${icon('star')}</button><input class="check" type="checkbox" ${S.selected.has(a.token)?'checked':''} aria-label="勾选 ${escape(a.name)}"></div><div class="card-body"><h3 class="card-title" title="${escape(a.name)}">${escape(a.name)}</h3><div class="card-meta">${cardMeta(a)}</div></div>`;
    }
    used.add(el);
    syncCard(el, a);
    if (el !== before) grid.insertBefore(el, before);
    before = el.nextElementSibling;
    const klass = 'card' + (a.token === S.current?.token ? ' current' : '') + (S.selected.has(a.token) ? ' checked' : '');
    if (el.className !== klass) el.className = klass;
    el.querySelector('.check').checked = S.selected.has(a.token);
    const geometry = `${i}:${S.columns}:${S.width}:${S.cell}:${S.coverHeight}`;
    if (el.dataset.geometry !== geometry) {
      el.style.cssText = `left:${i%S.columns*(S.width+12)}px;top:${Math.floor(i/S.columns)*S.cell}px;width:${S.width}px;height:${S.cell-12}px;--cover-height:${S.coverHeight}px`;
      el.dataset.geometry = geometry;
    }
    queueCover(a);
  }
  for (const el of [...grid.children]) if (!used.has(el)) el.remove();
  if (focused && document.activeElement?.closest('.card') !== focused) grid.querySelector(`[data-token="${focused.dataset.token}"]`)?.focus({
    preventScroll: true
  });
}

function toggleSelection(token) {
  S.selected.has(token) ? S.selected.delete(token) : S.selected.add(token);
  selectionUI();
  renderGrid();
}
async function inspect(a) {
  closeMoreMenu(false);
  const epoch = ++S.detailEpoch;
  if (S.inspectorPaneFor !== a.internal_id) {
    S.inspectorPane = 'description';
    S.inspectorPaneFor = a.internal_id;
    S.fileState = null;
    S.fileNoteEdit = null;
  }
  S.current = a;
  S.details = null;
  a.size = null;
  const anchor = browseAnchor();
  $('inspector').hidden = false;
  drawInspector(a, null);
  reflow(anchor);
  try {
    const detail = await rpc('detail', {
      token: a.token
    });
    if (epoch !== S.detailEpoch || S.current?.token !== a.token) return;
    S.details = detail;
    a.size = detail.size;
    drawInspector(a, detail);
  } catch (error) {
    if (epoch === S.detailEpoch) $('inspector-description').textContent = '详情读取失败：' + error.message;
  }
}

function manageableModFiles(detail) {
  if (!detail || detail.files == null || detail.files === '') return [];
  let data = detail.files;
  if (typeof data === 'string') {
    try { data = JSON.parse(data); } catch { return []; }
  }
  const files = Array.isArray(data) ? data : (data && Array.isArray(data.files) ? data.files : []);
  return files.filter(file => file && typeof file === 'object' && !['path', 'filename', 'name', 'display_name'].some(key => String(file[key] || '').includes('历史版本')));
}

function inspectorPlatform(asset) {
  const name = String(asset?.platform || '').toLowerCase();
  if (name.includes('nexus')) return 'nexus';
  if (name.includes('github')) return 'github';
  if (name.includes('steam')) return 'steam';
  if (name.includes('mod.io') || name.includes('modio')) return 'modio';
  return 'other';
}

function fileRecordName(file) {
  const filename = String(file.filename || '').trim();
  if (filename) return filename;
  const path = String(file.path || '').trim().replace(/\\/g, '/');
  const base = path ? path.split('/').pop() : '';
  return base || String(file.id || '文件');
}

function fileSelected(file) {
  if (file.selected_for_deploy !== undefined && file.selected_for_deploy !== null) return !!file.selected_for_deploy;
  return !!file.enabled;
}

function nexusCategory(file) {
  const meta = file.metadata || {};
  let raw = '';
  for (const key of ['category', 'nexus_category', 'category_name']) {
    if (meta[key]) { raw = String(meta[key]); break; }
  }
  if (!raw) {
    const parts = String(file.path || '').replace(/\\/g, '/').split('/').filter(part => part && part !== '.' && part !== '..');
    if (parts.length >= 2) {
      const folder = parts[0];
      const lower = folder.toLowerCase();
      if (folder && !['.info', 'info', '历史版本'].includes(folder) && !lower.endsWith('.zip') && !lower.endsWith('.7z') && !lower.endsWith('.rar')) raw = folder;
    }
  }
  if (!raw) raw = {nexus_main:'Main', nexus_optional:'Optional', nexus_misc:'Miscellaneous', nexus_old:'Old'}[file.file_role] || String(file.type || '');
  const text = raw.toLowerCase();
  if (text.includes('main')) return 'Main';
  if (text.includes('optional')) return 'Optional';
  if (text.includes('miscellaneous')) return 'Miscellaneous';
  if (raw.includes('汉化')) return '汉化';
  return 'Other';
}

function fileChoice(file, platform) {
  if (platform === 'nexus') {
    return {kind:'category', value:nexusCategory(file), options:[['Main','Main'],['Optional','Opt'],['Miscellaneous','Misc'],['汉化','汉化'],['Other','Other']]};
  }
  const role = String(file.file_role || '');
  const options = platform === 'github' ? [['main','Release'],['source','Src'],['other','Other']] : [['main','Main'],['source','Src'],['other','Other']];
  if (role === 'github_developer_build') return {kind:'role', value:'', extra:['', 'Development'], options};
  if (role === 'github_source_archive') return {kind:'role', value:'source', options};
  if (role === 'github_release_asset' || role === 'steam_content' || role === 'nexus_main') return {kind:'role', value:'main', options};
  if (role === 'nexus_optional') return {kind:'role', value:'', extra:['', 'Opt'], options};
  if (role === 'nexus_misc') return {kind:'role', value:'', extra:['', 'Misc'], options};
  if (role === 'nexus_old') return {kind:'role', value:'', extra:['', 'Old'], options};
  return {kind:'role', value:'other', options};
}

function sortInspectorFiles(files, platform) {
  const rank = file => {
    if (platform === 'nexus') return {Main:0, Optional:1, Miscellaneous:2, '汉化':3, Other:4}[nexusCategory(file)] ?? 4;
    const role = String(file.file_role || '');
    if (role === 'github_release_asset' || role === 'nexus_main' || role === 'steam_content') return 0;
    if (role === 'github_source_archive') return 2;
    return 1;
  };
  return files.slice().sort((a, b) => rank(a) - rank(b) || fileRecordName(a).localeCompare(fileRecordName(b), undefined, {sensitivity:'base'}));
}

function fileNote(file) {
  const meta = file.metadata || {};
  return String(meta.description || '').trim();
}

function displayedTypeLabel(choice) {
  if (choice.extra) return choice.extra[1];
  const match = choice.options.find(([value]) => value === choice.value);
  return match ? match[1] : 'Other';
}

function fileTypeTone(label) {
  return {Main:'main', Opt:'opt', Misc:'misc', Other:'other', '汉化':'locale', Release:'release', Src:'src', Development:'development', Old:'old'}[label] || 'other';
}

function rememberFileNoteDraft() {
  if (!S.fileNoteEdit) return;
  const input = document.querySelector('#inspector .inspector-file-editor textarea');
  if (input) S.fileNoteEdit.draft = input.value;
}

function inspectorFileMarkup(files, asset, disabled) {
  const platform = inspectorPlatform(asset);
  const rows = sortInspectorFiles(files, platform).map(file => {
    const label = fileRecordName(file);
    const id = String(file.id || '');
    const choice = fileChoice(file, platform);
    const typeLabel = displayedTypeLabel(choice);
    const locked = disabled || !id ? 'disabled' : '';
    const note = fileNote(file);
    const editing = !!(S.fileNoteEdit && S.fileNoteEdit.id === id);
    const noteHtml = !editing && note ? `<p class="inspector-file-note" title="${escape(note)}">${escape(note)}</p>` : '';
    const editorHtml = editing ? `<div class="inspector-file-editor"><textarea data-file-note-input maxlength="10000" aria-label="${escape('文件说明 ' + label)}">${escape(S.fileNoteEdit.draft)}</textarea><div class="inspector-file-editor-actions"><button type="button" data-file-note-cancel>取消</button><button type="button" data-file-note-save>保存</button></div></div>` : '';
    const tone = fileTypeTone(typeLabel);
    const menuOptions = choice.extra ? [choice.extra, ...choice.options] : choice.options;
    const currentValue = choice.extra ? choice.extra[0] : choice.value;
    return `<article class="inspector-file"><div class="inspector-file-row" role="listitem" data-file-id="${escape(id)}" data-file-name="${escape(label)}"><input type="checkbox" data-file-select aria-label="${escape('部署选择 ' + label)}" ${fileSelected(file) ? 'checked' : ''} ${locked}><span class="inspector-file-name" title="${escape(label)}">${escape(label)}</span><button type="button" class="inspector-file-type" data-tone="${tone}" data-kind="${choice.kind}" data-value="${escape(currentValue)}" data-options="${escape(JSON.stringify(menuOptions))}" aria-haspopup="listbox" aria-expanded="false" aria-controls="file-type-menu" aria-label="${escape('文件类型 ' + label)}" ${locked}><span class="inspector-file-type-label">${escape(typeLabel)}</span><span class="inspector-file-caret" aria-hidden="true">${icon('chevron-down')}</span></button><button type="button" class="inspector-file-edit" data-file-note aria-label="${escape('编辑说明 ' + label)}" title="编辑说明" ${locked}>${icon('pencil')}</button></div>${noteHtml}${editorHtml}</article>`;
  }).join('');
  return `<p id="inspector-files-status" class="inspector-files-status" role="status"></p>${rows}`;
}

function inspectorReading(a, d, prefix, viewOnly) {
  const files = viewOnly ? [] : manageableModFiles(d);
  const multi = files.length > 1;
  const filesOpen = multi && S.inspectorPane === 'files' && S.inspectorPaneFor === a.internal_id;
  const title = filesOpen ? '文件管理' : '描述摘要';
  const count = filesOpen ? `<span class="inspector-file-count">(${files.length})</span>` : '';
  const head = multi
    ? `<div class="inspector-section-head"><h3 class="section-line" id="${prefix}inspector-section-title"><span class="inspector-section-label">${title}</span>${count}</h3><button type="button" id="${prefix}inspector-file-toggle" class="inspector-file-toggle" aria-pressed="${filesOpen ? 'true' : 'false'}" aria-label="${filesOpen ? '返回描述摘要' : '切换到文件管理'}" title="${filesOpen ? '返回描述摘要' : '文件管理'}">${icon('swap')}</button></div>`
    : `<h3 class="section-line">描述摘要</h3>`;
  const fileBlock = multi ? `<div id="${prefix}inspector-files" class="inspector-files" role="list" aria-label="Mod 文件"${filesOpen ? '' : ' hidden'}>${inspectorFileMarkup(files, a, viewOnly || S.readOnly || S.busy || !d)}</div>` : '';
  return `${head}<div class="description inspector-summary" id="${prefix}inspector-description"${filesOpen ? ' hidden' : ''}></div>${fileBlock}`;
}

function inspectorContent(a, d, prefix = '', viewOnly = false) {
  const deployBadge = d?.actions?.state === 'deployed' ? '<span class="badge deployed">已部署</span>' : '';
  return `<div class="inspector-scroll"><div class="inspector-overview"><div class="inspector-cover-wrap"><div id="${prefix}inspector-image">${coverMarkup(a,'inspector-cover')}</div></div><div class="inspector-identity"><h2 class="inspector-name">${escape(a.name)}</h2><p class="inspector-source"><span>${escape(a.platform||'来源未知')}</span>${a.size==null?'':`<span class="inspector-size">${size(a.size)}</span>`}</p>${deployBadge}</div></div><div class="inspector-type-property"><span>Mod 类型</span><button id="${prefix}inspector-type-action" class="inspector-type-value" title="${a.type_id==null?'设置 Mod 类型':escape(a.type)}" aria-label="Mod 类型：${a.type_id==null?'未设置，设置类型':escape(a.type)}" aria-haspopup="listbox" aria-controls="inspector-type-menu" aria-expanded="false" ${viewOnly||S.readOnly||S.busy||!d?'disabled':''}><span class="inspector-type-name">${a.type_id==null?'未设置':escape(a.type)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg></button></div>${d?.tags?.length?`<div class="inspector-user-tags" aria-label="用户标签">${icon('tags')}<div class="inspector-tag-values">${d.tags.map(tag=>`<span class="inspector-tag">${escape(tag)}</span>`).join('')}</div></div>`:''}<dl class="facts inspector-workspace"><dt>Workspace ID</dt><dd>${escape(a.workspace_id||'未知')}</dd></dl>${inspectorReading(a, d, prefix, viewOnly)}</div>`;
}

// Type commands still go through the existing setters. Those setters also
// rewrite selection, so a following file-select restores the checkbox.
async function persistInspectorFile(action, values) {
  const asset = S.current;
  if (!asset || S.readOnly || S.busy) return;
  rememberFileNoteDraft();
  const epoch = S.detailEpoch;
  const context = {game:$('game').value, snapshot:S.snapshot, token:asset.token, internal_id:asset.internal_id};
  const currentFile = manageableModFiles(S.details).find(file => String(file.id) === String(values.file_id));
  const keepSelection = (action === 'file-role' || action === 'file-category') && currentFile ? fileSelected(currentFile) : null;
  S.busy = true;
  closeFileTypeMenu(false);
  document.querySelectorAll('#inspector-files input, #inspector-files .inspector-file-type, #inspector-files .inspector-file-edit, #inspector-file-toggle').forEach(el => { el.disabled = true; });
  try {
    let saved = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      if (!S.fileState || S.fileState.internal_id !== asset.internal_id) {
        const data = await rpc('asset-management', context);
        if (epoch !== S.detailEpoch || S.current?.internal_id !== asset.internal_id) return;
        S.fileState = {internal_id:asset.internal_id, data};
      }
      try {
        saved = await rpc(action, {...context, version:S.fileState.data.version, ...values}, true);
        break;
      } catch (error) {
        S.fileState = null;
        if (attempt === 1 || !String(error.message || '').includes('已变化')) throw error;
      }
    }
    if (!saved || epoch !== S.detailEpoch || S.current?.internal_id !== asset.internal_id) return;
    if (keepSelection !== null) {
      const updated = (saved.files?.files || []).find(file => String(file.id) === String(values.file_id));
      if (updated && fileSelected(updated) !== keepSelection) {
        saved = await rpc('file-select', {...context, version:saved.version, file_id:values.file_id, value:keepSelection}, true);
      }
    }
    S.fileState = {internal_id:asset.internal_id, data:saved};
    if (action === 'file-description') S.fileNoteEdit = null;
    if (S.details) S.details.files = JSON.stringify(saved.files);
    const top = document.querySelector('#inspector .inspector-scroll')?.scrollTop || 0;
    S.busy = false;
    drawInspector(S.current, S.details);
    const scroller = document.querySelector('#inspector .inspector-scroll');
    if (scroller) scroller.scrollTop = top;
  } catch (error) {
    if (epoch === S.detailEpoch && S.current?.internal_id === asset.internal_id) {
      S.busy = false;
      drawInspector(S.current, S.details);
      const note = $('inspector-files-status');
      if (note) note.textContent = error.message || '保存失败';
    }
  } finally {
    S.busy = false;
    syncFavoriteControls();
  }
}

function renderInspectorDescription(id, d) {
  // Parse sanitized descriptions separately so unclosed markup cannot absorb surrounding controls.
  $(id).innerHTML = d ? d.html||'暂无描述。现有来源未提供内容。' : '正在读取已验证的描述来源…';
}

function closeFileTypeMenu(restoreFocus = true) {
  const menu = $('file-type-menu');
  if (!menu?.matches(':popover-open')) return;
  const trigger = document.querySelector('.inspector-file-type[aria-expanded="true"]');
  menu.hidePopover();
  if (trigger) trigger.setAttribute('aria-expanded', 'false');
  if (restoreFocus) trigger?.focus({preventScroll: true});
}

function placeFileTypeMenu(menu, trigger) {
  const anchor = trigger.getBoundingClientRect();
  const bounds = menu.getBoundingClientRect();
  const gap = 6;
  const footer = document.querySelector('#inspector .inspector-footer')?.getBoundingClientRect();
  const header = document.querySelector('#inspector .inspector-top')?.getBoundingClientRect();
  const floor = footer ? footer.top - 4 : innerHeight - 8;
  const ceiling = header ? header.bottom + 4 : 8;
  const spaceBelow = floor - anchor.bottom;
  const spaceAbove = anchor.top - ceiling;
  const openUp = spaceBelow < bounds.height + gap && spaceAbove >= spaceBelow;
  const top = openUp ? anchor.top - bounds.height - gap : anchor.bottom + gap;
  const left = anchor.right - bounds.width;
  menu.dataset.placement = openUp ? 'up' : 'down';
  menu.style.left = Math.max(8, Math.min(left, innerWidth - bounds.width - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(top, innerHeight - bounds.height - 8)) + 'px';
}

function openFileTypeMenu(button) {
  const menu = $('file-type-menu');
  const row = button.closest('[data-file-id]');
  if (!menu || !row || button.disabled) return;
  closeInspectorTypeMenu(false);
  if (menu.matches(':popover-open') && menu.dataset.fileId === row.dataset.fileId) {
    closeFileTypeMenu();
    return;
  }
  closeFileTypeMenu(false);
  const options = JSON.parse(button.dataset.options || '[]');
  menu.dataset.fileId = row.dataset.fileId;
  menu.dataset.kind = button.dataset.kind;
  menu.innerHTML = options.map(([value, text]) => {
    const selected = String(value) === button.dataset.value;
    const check = selected ? icon('check') : '<span class="file-type-check-space"></span>';
    return `<button type="button" role="option" tabindex="-1" data-value="${escape(value)}" aria-selected="${selected}"><span class="file-type-dot" data-tone="${fileTypeTone(text)}"></span><span>${escape(text)}</span>${check}</button>`;
  }).join('');
  menu.onclick = event => {
    const option = event.target.closest('[role="option"]');
    if (!option) return;
    const value = option.dataset.value;
    const same = option.getAttribute('aria-selected') === 'true';
    const fileId = menu.dataset.fileId;
    const kind = menu.dataset.kind;
    closeFileTypeMenu(false);
    if (same || !value) return;
    persistInspectorFile(kind === 'category' ? 'file-category' : 'file-role', {file_id:fileId, value});
  };
  button.setAttribute('aria-expanded', 'true');
  menu.showPopover();
  placeFileTypeMenu(menu, button);
  (menu.querySelector('[aria-selected="true"]') || menu.querySelector('[role="option"]'))?.focus({preventScroll: true});
}

function drawInspector(a, d) {
  const policy = d?.actions;
  const primary = policy?.primary || {id: 'unconfirmed', label: '确认状态中', icon: 'circle-alert', reason: '正在读取权威部署状态；只读预览不可执行写入'};
  const edit = policy?.edit || {id: 'edit', label: '编辑', icon: 'pencil', reason: '只读预览：编辑信息会写入 SQLite'};
  const command = (action, main = false) => `<button class="inspector-command ${main?'primary':''}" data-command="${action.id}" ${action.enabled && !S.busy ? "" : "disabled"} title="${escape(action.reason)}" aria-label="${escape(action.label+'，'+action.reason)}">${icon(action.icon)}<span>${escape(action.label)}</span></button>`;
  $('inspector').innerHTML = `<div class="inspector-top"><span>资产详情</span><button id="open-detail" class="inspector-detail-link" ${d?'':'disabled'}>${icon('book-open')}完整详情</button><button id="close-inspector" class="icon-button" aria-label="关闭详情侧栏">${icon('close')}</button></div>${inspectorContent(a, d)}<div class="inspector-footer"><div class="inspector-command-row">${command(primary,true)}${policy?.undeploy?command(policy.undeploy):''}${command(edit)}<button id="inspector-more" class="inspector-more" title="更多操作" aria-label="更多操作" aria-haspopup="menu" aria-controls="more-menu" aria-expanded="false" ${policy?'':'disabled'}>${icon('more-horizontal')}</button></div></div>`;
  renderInspectorDescription('inspector-description', d);
  $('close-inspector').onclick = closeInspector;
  $('open-detail').onclick = openDetail;
  $('inspector-more').onclick = openMoreMenu;
  document.querySelector('[data-command=edit]').onclick = openEditor;
  for (const button of document.querySelectorAll('[data-command]')) {
    if (['deploy','redeploy','undeploy'].includes(button.dataset.command)) button.onclick=()=>runCommand(button.dataset.command,{});
  }
  if($('inspector-type-action'))$('inspector-type-action').onclick=openInspectorTypePicker;
  if($('inspector-file-toggle'))$('inspector-file-toggle').onclick=()=>{
    rememberFileNoteDraft();
    S.inspectorPane = S.inspectorPane === 'files' ? 'description' : 'files';
    S.inspectorPaneFor = a.internal_id;
    drawInspector(a, d);
  };
  closeFileTypeMenu(false);
  closeInspectorTypeMenu(false);
  document.querySelector('#inspector .inspector-scroll')?.addEventListener('scroll', () => { closeFileTypeMenu(false); closeInspectorTypeMenu(false); }, {passive:true});
  if($('inspector-files')){
    $('inspector-files').onchange=event=>{
      const row = event.target.closest('[data-file-id]');
      if (!row || S.readOnly || S.busy) return;
      if (event.target.matches('[data-file-select]')) persistInspectorFile('file-select', {file_id:row.dataset.fileId, value:event.target.checked});
    };
    $('inspector-files').onclick=event=>{
      const item = event.target.closest('.inspector-file');
      if (!item || S.readOnly || S.busy) return;
      const fileId = item.querySelector('[data-file-id]')?.dataset.fileId || '';
      if (event.target.closest('[data-file-note-cancel]')) {
        S.fileNoteEdit = null;
        drawInspector(a, d);
        return;
      }
      if (event.target.closest('[data-file-note-save]')) {
        const value = item.querySelector('[data-file-note-input]')?.value ?? '';
        persistInspectorFile('file-description', {file_id:fileId, value});
        return;
      }
      if (event.target.closest('.inspector-file-type')) {
        openFileTypeMenu(event.target.closest('.inspector-file-type'));
        return;
      }
      if (event.target.closest('[data-file-note]')) {
        if (S.fileNoteEdit && S.fileNoteEdit.id !== fileId) {
          document.querySelector('#inspector .inspector-file-editor textarea')?.focus();
          return;
        }
        const note = item.querySelector('.inspector-file-note');
        S.fileNoteEdit = {id:fileId, draft:note ? note.getAttribute('title') || '' : ''};
        const top = document.querySelector('#inspector .inspector-scroll')?.scrollTop || 0;
        drawInspector(a, d);
        const scroller = document.querySelector('#inspector .inspector-scroll');
        if (scroller) scroller.scrollTop = top;
        document.querySelector('#inspector .inspector-file-editor textarea')?.focus();
      }
    };
  }
  queueCover(a);
}

function positionMenu(menu, trigger, above = false) {
  const anchor = trigger.getBoundingClientRect();
  const bounds = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(anchor.right - bounds.width, innerWidth - bounds.width - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(above ? anchor.top - bounds.height - 8 : anchor.bottom + 8,
    innerHeight - bounds.height - 8)) + 'px';
}

function closeMoreMenu(restoreFocus = true) {
  if (!$('more-menu').matches(':popover-open')) return;
  $('more-menu').hidePopover();
  if (restoreFocus) $('inspector-more')?.focus({preventScroll: true});
}

function focusMenuItem(menu, index) {
  const items = [...menu.querySelectorAll('[role="menuitem"], [role="option"]')].filter(el=>!el.hidden);
  if (!items.length) return;
  const item = items[(index + items.length) % items.length];
  item.focus({preventScroll: true});
  item.scrollIntoView({block: 'nearest'});
  if (item.dataset.reason) $('more-menu-reason').textContent = item.dataset.reason;
}

function openMoreMenu() {
  const menu = $('more-menu');
  if (menu.matches(':popover-open')) return closeMoreMenu();
  const actions = S.details?.actions?.menu;
  if (!actions) return;
  $('more-menu-items').innerHTML = actions.map(action => `<button role="menuitem" tabindex="-1" aria-disabled="${!action.enabled}" class="${action.id==='mod-deregister'?'danger-menu-item':''}" data-action="${action.id}" data-reason="${escape(action.reason)}" title="${escape(action.reason)}">${icon(action.icon)}<span>${escape(action.label)}</span><span class="menu-disabled-mark">${action.enabled?'':'不可用'}</span></button>`).join('');
  // Disabled items stay focusable for reading reasons and keyboard navigation.
  // Only explicitly admitted actions have handlers; the bridge checks permission again.
  $('more-menu-items').onclick = event => {
    const item = event.target.closest('[role="menuitem"]');
    if (item) { event.preventDefault(); if(item.dataset.action==='mod-deregister'&&item.getAttribute('aria-disabled')==='false'){closeMoreMenu();openDeregistration();} else if(item.dataset.action==='asset-management'&&item.getAttribute('aria-disabled')==='false'){closeMoreMenu();openAssetManagement();} else if(['open-source','open-offline','import-offline','save-offline','cover','refresh-metadata'].includes(item.dataset.action)&&item.getAttribute('aria-disabled')==='false'){closeMoreMenu();runInspectorOperation(item.dataset.action);} else if(['content-add','content-rescan','open-folder'].includes(item.dataset.action)&&item.getAttribute('aria-disabled')==='false'){closeMoreMenu();registrationAssetAction(item.dataset.action==='open-folder'?'content-open':item.dataset.action);} else if(item.dataset.action==='tags' && item.getAttribute('aria-disabled')==='false') { closeMoreMenu(); openTags(); } else $('more-menu-reason').textContent = item.dataset.reason; }
  };
  $('more-menu-reason').textContent = actions[0].reason;
  document.querySelector('#more-menu .menu-caption').textContent=S.readOnly?'更多操作 · 只读预览':'更多操作';
  menu.showPopover();
  positionMenu(menu, $('inspector-more'), true);
  focusMenuItem(menu, 0);
}

function syncGamePicker() {
  const option = $('game').selectedOptions[0];
  $('game-picker-label').textContent = option?.text || '暂无游戏';
  $('game-picker').title = option?.text || '暂无游戏';
  $('game-picker').disabled = false;
}

function closeGameMenu(restoreFocus = true) {
  if (!$('game-menu').matches(':popover-open')) return;
  $('game-menu').hidePopover();
  if (restoreFocus) $('game-picker').focus({preventScroll: true});
}

function openGameMenu() {
  const menu = $('game-menu');
  if (menu.matches(':popover-open')) return closeGameMenu();
  menu.innerHTML = '<div class="game-search"><input id="game-search" placeholder="搜索游戏或 AppID" aria-label="搜索游戏"></div><div class="game-options" id="game-options">'+[...$('game').options].map(option => `<button role="option" tabindex="-1" data-game="${escape(option.value)}" aria-selected="${option.selected}">${icon('game')}<span>${escape(option.text)}</span>${option.selected?icon('check'):'<span class="option-check-space"></span>'}</button>`).join('')+'</div><div class="game-menu-footer"><button type="button" data-manage-game>'+icon('pencil')+'管理当前游戏</button>'+(!S.readOnly?'<button type="button" data-add-game><span>＋</span>添加游戏</button>':'')+'</div>';
  $('game-search').oninput=()=>{const q=$('game-search').value.toLocaleLowerCase();for(const b of $('game-options').children)b.hidden=!(b.textContent+b.dataset.game).toLocaleLowerCase().includes(q);};
  menu.onclick = event => {
    if(event.target.closest('[data-add-game]'))return openCreateGame();
    if(event.target.closest('[data-manage-game]')){closeGameMenu();return $('game-settings').click();}
    const option = event.target.closest('[role="option"]');
    if (!option) return;
    const changed = $('game').value !== option.dataset.game;
    $('game').value = option.dataset.game;
    syncGamePicker();
    closeGameMenu();
    if (changed) $('game').dispatchEvent(new Event('change'));
  };
  menu.showPopover();
  positionMenu(menu, $('game-picker'));
  $('game-search').focus();
}

$('game-picker').onclick = openGameMenu;
for (const [menuId, triggerId] of [['more-menu', 'inspector-more'], ['game-menu', 'game-picker']]) {
  $(menuId).addEventListener('toggle', event => $(triggerId)?.setAttribute('aria-expanded', String(event.newState === 'open')));
}
document.addEventListener('pointerdown', event => {
  const menu = $('file-type-menu');
  if (menu?.matches(':popover-open') && !menu.contains(event.target) && !event.target.closest?.('.inspector-file-type')) closeFileTypeMenu(false);
  const typeMenu = $('inspector-type-menu');
  if (typeMenu?.matches(':popover-open') && !typeMenu.contains(event.target) && !event.target.closest?.('#inspector-type-action')) closeInspectorTypeMenu(false);
}, true);
document.addEventListener('keydown', event => {
  const fileMenu = $('file-type-menu');
  if (fileMenu?.matches(':popover-open')) {
    const items = [...fileMenu.querySelectorAll('[role="option"]')];
    const index = items.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation(); closeFileTypeMenu();
    } else if (event.key === 'Tab') {
      closeFileTypeMenu(false);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault(); event.stopImmediatePropagation();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : index + (event.key === 'ArrowDown' ? 1 : -1);
      items[(next + items.length) % items.length]?.focus({preventScroll: true});
    } else if ((event.key === 'Enter' || event.key === ' ') && document.activeElement?.getAttribute('role') === 'option') {
      event.preventDefault(); document.activeElement.click();
    }
    return;
  }
  const modTypeMenu = $('inspector-type-menu');
  if (modTypeMenu?.matches(':popover-open')) {
    const items = [...modTypeMenu.querySelectorAll('[role="option"]')];
    const index = items.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation(); closeInspectorTypeMenu();
    } else if (event.key === 'Tab') {
      closeInspectorTypeMenu(false);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault(); event.stopImmediatePropagation();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : index + (event.key === 'ArrowDown' ? 1 : -1);
      items[(next + items.length) % items.length]?.focus({preventScroll: true});
    } else if ((event.key === 'Enter' || event.key === ' ') && document.activeElement?.getAttribute('role') === 'option') {
      event.preventDefault(); document.activeElement.click();
    }
    return;
  }
  if (event.target.id === 'inspector-type-action' && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
    event.preventDefault(); openInspectorTypePicker();
    return;
  }
  const typeTrigger = event.target.closest?.('.inspector-file-type');
  if (typeTrigger && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
    event.preventDefault(); openFileTypeMenu(typeTrigger);
    return;
  }
  const menu = $('more-menu').matches(':popover-open') ? $('more-menu') : $('game-menu').matches(':popover-open') ? $('game-menu') : null;
  if (!menu) {
    if (event.target === $('game-picker') && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); openGameMenu();
    }
    return;
  }
  const close = menu.id === 'more-menu' ? closeMoreMenu : closeGameMenu;
  if(menu.id==='game-menu'&&event.target.id==='game-search'&&['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();focusMenuItem(menu,event.key==='ArrowDown'?0:-1);return;}
  if (event.key === 'Escape') {
    event.preventDefault(); event.stopImmediatePropagation(); close();
  } else if (event.key === 'Tab') {
    close();
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && event.target.id!=='game-search') {
    event.preventDefault(); event.stopImmediatePropagation();
    const items = [...menu.querySelectorAll('[role="menuitem"], [role="option"]')].filter(el=>!el.hidden);
    const index = items.indexOf(document.activeElement);
    focusMenuItem(menu, event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : index + (event.key === 'ArrowDown' ? 1 : -1));
  }
}, true);
window.addEventListener('resize', () => { closeMoreMenu(false); closeGameMenu(false); closeFileTypeMenu(false); });

function closeInspector() {
  if($('inspector-type-menu').matches(':popover-open'))$('inspector-type-menu').hidePopover();
  closeFileTypeMenu(false);
  closeMoreMenu(false);
  ++S.detailEpoch;
  S.current = null;
  S.details = null;
  const anchor = browseAnchor();
  $('inspector').hidden = true;
  reflow(anchor);
}

function openDetail() {
  closeMoreMenu(false);
  if (!S.details || !S.current) return;
  S.detailTab = 'description';
  drawDetail();
  $('detail').showModal();
  $('detail-close').focus();
}

function drawDetail() {
  const a = S.current,
    d = S.details;
  if (!a || !d) return;
  const tab = S.detailTab;
  $('detail').classList.toggle('short-detail', tab === 'description' && d.description.length < 650);
  const facts = [
    ['来源平台', a.platform],
    ['Mod 类型', a.type],
    ['本地大小', size(a.size)],
    ['部署登记', deploymentLabel(a)],
    ['收藏', a.favorite ? '已收藏' : '未收藏'],
    ['Workspace ID', a.workspace_id || '未知']
  ];
  let content = tab === 'description' ? `<h3>完整描述</h3><div class="description">${d.html||'<p>暂无描述。现有本地来源未提供内容。</p>'}</div><p class="quiet section-line">描述来源：${escape(d.source)} · ${d.description.length.toLocaleString()} 字符 · 嵌入图片和外部资源不自动加载</p>` : tab === 'metadata' ? `<h3>身份与元数据</h3><dl class="metadata">${[['Mod 名称',a.name],['Internal UUID',d.internal_id],['Workspace ID',d.workspace_id],['SQLite PK',String(d.mod_pk)],['来源 URL',d.source_url||'未知'],['描述来源',d.source],['用户备注',d.notes||'暂无备注']].map(([k,v])=>`<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl>` : `<h3>文件与本地资源</h3><p class="quiet">显示现有记录，不执行目录扫描或部署操作。</p><h3>管理目录</h3><div class="resources">${escape(d.path||'未知')}</div><h3>文件记录</h3><div class="resources">${escape(formatFiles(d.files))}</div>`;
  $('detail-content').innerHTML = `<header class="detail-header"><div id="detail-image" data-token="${a.token}">${coverMarkup(a,'detail-cover')}</div><div class="detail-heading"><span class="eyebrow">ASSET DETAILS · ${escape($('game').selectedOptions[0]?.text)}</span><h2>${escape(a.name)}</h2><div class="badges">${stateBadge(a)}<span class="badge">${escape(a.platform)}</span><span class="badge">${escape(a.type)}</span></div></div><button class="icon-button" id="detail-close" aria-label="关闭完整详情">${icon('close')}</button></header><nav class="detail-tabs">${[['description','描述'],['metadata','身份与元数据'],['resources','文件与资源']].map(([key,label])=>`<button class="tab ${key===tab?'active':''}" data-detail-tab="${key}">${label}</button>`).join('')}</nav><div class="detail-scroll"><div class="detail-columns"><article class="detail-reading">${content}</article><aside class="detail-facts"><h3>资产摘要</h3><dl class="facts">${facts.map(([k,v])=>`<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl><p class="quiet section-line">${S.readOnly?'安全只读<br>当前不提供业务写入或外部打开。':'隔离可写验收<br>已开放操作可在 Inspector 执行。'}</p></aside></div></div><footer class="detail-footer"><span>返回后保留查看对象、勾选集合与浏览位置</span><button id="detail-return">返回资产库</button></footer>`;
  $('detail-close').onclick = () => $('detail').close();
  $('detail-return').onclick = () => $('detail').close();
  document.querySelectorAll('[data-detail-tab]').forEach(el => el.onclick = () => {
    S.detailTab = el.dataset.detailTab;
    drawDetail();
  });
}

function formatFiles(value) {
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value || '暂无文件记录';
  }
}
let frame;

function scheduleGrid() {
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = null;
    const viewport = $('viewport');
    const first = Math.max(0, Math.floor(viewport.scrollTop / S.cell) - 1) * S.columns;
    const last = Math.min(S.rows.length, (Math.ceil((viewport.scrollTop + viewport.clientHeight) / S.cell) + 1) * S.columns);
    if (S.renderRange?.[0] === first && S.renderRange?.[1] === last) return;
    renderGrid();
  });
}
$('viewport').addEventListener('scroll', scheduleGrid);
new ResizeObserver(() => reflow(browseAnchor())).observe($('viewport'));
$('grid').addEventListener('click', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  const a = S.rows.find(a => a.token === card.dataset.token);
  if(e.target.closest('[data-favorite]')){e.stopPropagation();runCommand('favorite-set',{value:!a.favorite},{asset:a,snapshot:S.snapshot,game:$('game').value,epoch:S.epoch});return;}
  if (e.target.matches('.check')) {
    toggleSelection(a.token);
    S.selectionAnchor=a.token;
    return;
  }
  if(e.shiftKey){
    const start=S.rows.findIndex(row=>row.token===S.selectionAnchor),end=S.rows.findIndex(row=>row.token===a.token);
    if(!e.ctrlKey)S.selected.clear();
    for(const row of S.rows.slice(Math.min(start<0?end:start,end),Math.max(start<0?end:start,end)+1))S.selected.add(row.token);
    selectionUI();renderGrid();return;
  }
  if(e.ctrlKey||e.metaKey){toggleSelection(a.token);S.selectionAnchor=a.token;return;}
  S.selectionAnchor=a.token;
  inspect(a);
});
$('grid').addEventListener('dblclick', e => {
  const card = e.target.closest('.card');
  if(e.target.closest('[data-favorite],.check'))return;
  if (card?.dataset.token === S.current?.token && S.details) openDetail();
});
$('grid').addEventListener('keydown', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  const index = S.rows.findIndex(a => a.token === card.dataset.token);
  if(e.target.closest('[data-favorite],.check'))return;
  if (e.key === ' ') {
    e.preventDefault();
    toggleSelection(card.dataset.token);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    inspect(S.rows[index]);
  } else {
    const delta = {
      ArrowRight: 1,
      ArrowLeft: -1,
      ArrowDown: S.columns,
      ArrowUp: -S.columns
    } [e.key];
    if (delta) {
      e.preventDefault();
      const next = Math.max(0, Math.min(S.rows.length - 1, index + delta));
      const y = Math.floor(next / S.columns) * S.cell;
      if (y < $('viewport').scrollTop) $('viewport').scrollTop = y;
      else if (y + S.cell > $('viewport').scrollTop + $('viewport').clientHeight) $('viewport').scrollTop = y + S.cell - $('viewport').clientHeight;
      renderGrid();
      $('grid').querySelector(`[data-token="${S.rows[next].token}"]`)?.focus({
        preventScroll: true
      });
    }
  }
});
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && document.activeElement?.closest('#viewport')) {
    e.preventDefault();
    S.selected = new Set(S.rows.map(a => a.token));
    selectionUI();
    renderGrid();
  }
  if (e.key === 'Escape' && !$('detail').open) closeInspector();
});
document.addEventListener('click', e => {
  if (e.target.closest('.description a')) e.preventDefault();
});
$('select-all').onclick = () => {
  S.selected = new Set(S.rows.map(a => a.token));
  selectionUI();
  renderGrid();
};
$('clear-selection').onclick = () => {
  S.selected.clear();
  selectionUI();
  renderGrid();
};
let debounce;
$('search').oninput = () => {
  clearTimeout(debounce);
  debounce = setTimeout(filter, 120);
};
for (const id of ['status', 'type', 'collection','library-sort','platform']) $(id).onchange = filter;
$('clear-search').onclick = () => {
  $('search').value = '';
  filter();
};
$('reset-filter').onclick = () => {
  $('search').value = '';
  $('status').value = 'all';
  $('type').value = '';
  $('collection').value = '';
  $('platform').value = '';
  filter();
};
document.querySelectorAll('[data-browse]').forEach(button => button.onclick = () => {
  S.browse = button.dataset.browse;
  document.querySelectorAll('[data-browse]').forEach(el => el.classList.toggle('active', el === button));
  $('collection').hidden = true;
  $('collection').value='';
  filter();
});
for (const id of ['standard', 'compact']) $(id).onclick = () => {
  const anchor = browseAnchor();
  S.compact = id === 'compact';
  $('compact').classList.toggle('active', S.compact);
  $('standard').classList.toggle('active', !S.compact);
  reflow(anchor);
};
$('refresh').onclick = load;
$('game').onchange = () => {
  $('editor').close();
  $('organization').close();
  $('collection').value='';
  $('search').value = '';
  $('status').value = 'all';
  $('type').value = '';
  ++S.detailEpoch;
  load();
};
$('library-nav').onclick = () => {
  $('search').focus();
};
new QWebChannel(qt.webChannelTransport, async channel => {
  bridge = channel.objects.library;
  bridge.response.connect(raw => {
    const message = JSON.parse(raw),
      pending = requests.get(message.request);
    if (!pending) return;
    requests.delete(message.request);
    bridge.acknowledge(message.request);
    message.error ? pending.reject(new Error(message.error)) : pending.resolve(message.result);
  });
  try {
    const result = await rpc('bootstrap');
    S.readOnly = result.readOnly;
    S.registrationSources=result.registrationSources;
    S.registrationPreferences=result.registrationPreferences||{};
    S.deployCommands = result.deployCommands;
    S.organizeCommands = result.organizeCommands;
    if (!S.readOnly) { document.querySelector('.mode').textContent='正常模式';document.querySelector('.rail-note').textContent=S.organizeCommands?'已开放标签、合集、部署、收藏与用户信息编辑。':S.deployCommands?'已开放部署、收藏与用户信息编辑。':'收藏与用户信息编辑已接通。其他业务仍待迁移。'; if(S.deployCommands)document.querySelector('.readonly-note').textContent='本地资源 · 按业务权限执行';$('connection-status').textContent='本地数据库 · 已连接';document.querySelector('.statusbar span:last-child').textContent=S.deployCommands?'本地数据 · 部署与编辑可执行':'本地数据 · 收藏与用户信息编辑可执行'; }
    $('game').replaceChildren(...result.games.map(g => new Option(g.name, g.key)));
    syncGamePicker();
    if (result.games.length) {
      $('game').value = (result.games.find(g=>g.name===result.session?.game_name) || result.games.find(g => g.key !== '0') || result.games[0]).key;
      await load();
      showSpace(({0:'acquire',1:'library',2:'deployment'})[result.session?.page]||'library');
    } else {
      $('results').textContent = '数据库中没有游戏记录';
      $('summary').textContent = '0 个 Mod';
    }
  } catch (error) {
    $('results').hidden = false;
    $('results').textContent = '连接失败：' + error.message;
  }
});

let editingAsset;
function openEditor() {
  if (S.readOnly || S.busy || !S.details || !S.current) return;
  editingAsset = {asset: S.current, snapshot: S.snapshot, game: $('game').value, epoch: S.epoch};
  $('edit-name').value = S.details.editable.display_name;
  $('edit-description').value = S.details.editable.custom_description;
  $('edit-notes').value = S.details.editable.user_notes;
  if(typeof loadAdvancedEditor==='function')loadAdvancedEditor();
  $('edit-error').textContent = '';
  $('editor').showModal();
  if(typeof loadEditorTypes==='function')loadEditorTypes();
  $('edit-name').focus();
}
$('edit-cancel').onclick = () => $('editor').close();
$('edit-form').onsubmit = async event => {
  event.preventDefault();
  const target = editingAsset;
  $('edit-save').disabled = true;
  const ok = await runCommand('metadata-save', {fields: {display_name: $('edit-name').value, custom_description: $('edit-description').value, user_notes: $('edit-notes').value,...(typeof advancedEditorValues==='function'?advancedEditorValues():{})}}, target);
  $('edit-save').disabled = false;
  if (ok && editingAsset === target) $('editor').close();
};
function syncFavoriteControls(){for(const b of document.querySelectorAll('[data-favorite]'))b.disabled=S.readOnly||S.busy;if($('batch-edit'))$('batch-edit').disabled=S.readOnly||S.busy;}
async function runCommand(action, values, target = {asset:S.current,snapshot:S.snapshot,game:$('game').value,epoch:S.epoch}) {
  if (S.readOnly || S.busy || !target.asset) return false;
  S.busy = true;
  if (S.current && S.details) drawInspector(S.current, S.details);
  $('results').hidden = false; $('results').textContent = ({deploy:'正在部署…',redeploy:'正在重新部署…',undeploy:'正在取消部署…'})[action] || '正在保存…';
  try {
    const result = await rpc(action, {...values,token:target.asset.token,internal_id:target.asset.internal_id,game:target.game,snapshot:target.snapshot}, true);
    if (result.internal_id !== target.asset.internal_id || result.game !== target.game) throw new Error('响应身份不匹配');
    if (target.epoch === S.epoch && target.game === $('game').value) {
      if (!await refreshCommandView()) return true;
      $('results').hidden = false; $('results').textContent = ['deploy','redeploy','undeploy'].includes(action)?'操作成功':'保存成功';
    }
    return true;
  } catch (error) {
    if (target.epoch === S.epoch && target.game === $('game').value) { if (['deploy','redeploy','undeploy'].includes(action) && !await refreshCommandView()) return false; $('results').hidden=false;$('results').textContent=(['deploy','redeploy','undeploy'].includes(action)?'操作失败：':'保存失败：')+error.message; if ($('editor').open) $('edit-error').textContent=error.message; }
    return false;
  } finally { S.busy=false;syncFavoriteControls(); if(S.current && S.details)drawInspector(S.current,S.details); }
}

async function refreshCommandView() {
  const game = $('game').value;
  return await load() === true && $('game').value === game;
}

$('library-filters').onclick=()=>{
 const menu=$('library-filter-menu');if(menu.matches(':popover-open'))menu.hidePopover();else{menu.showPopover();positionMenu(menu,$('library-filters'));$('platform').focus();}
};
$('library-filter-menu').addEventListener('toggle',()=>{$('library-filters').setAttribute('aria-expanded',String($('library-filter-menu').matches(':popover-open')));});
$('platform-clear').onclick=()=>{$('platform').value='';filter();};
