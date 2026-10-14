'use strict';
const paths = {
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  dense: 'M3 3h4v4H3z M10 3h4v4h-4z M17 3h4v4h-4z M3 10h4v4H3z M10 10h4v4h-4z M17 10h4v4h-4z M3 17h4v4H3z M10 17h4v4h-4z M17 17h4v4h-4z',
  layers: 'M3 8l9-5 9 5-9 5z M3 12l9 5 9-5 M3 16l9 5 9-5',
  download: 'M12 3v12 M7 10l5 5 5-5 M4 17v4h16v-4',
  shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',
  search: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  game: 'M6 7h12l3 10-3 2-4-4h-4l-4 4-3-2z M6 10v4 M4 12h4 M16 11h1 M18 13h1',
  refresh: 'M20 7a9 9 0 1 0 1 7 M20 3v5h-5',
  close: 'M5 5l14 14 M19 5L5 19',
  image: 'M3 4h18v16H3z M3 16l5-5 4 4 3-3 6 6 M14 8h1'
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
  details: null
};
let bridge, serial = 0;
const requests = new Map();

function rpc(operation, args = {}) {
  return new Promise((resolve, reject) => {
    const id = String(++serial);
    requests.set(id, {
      resolve,
      reject
    });
    bridge.request(id, operation, JSON.stringify(args));
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
const stateBadge = a => `<span class="badge ${a.deployed?'deployed':''}">${a.deployed?'已部署':'未部署'}</span>`;

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
async function load() {
  const epoch = ++S.epoch;
  $('refresh').disabled = true;
  $('results').textContent = '正在读取数据库快照…';
  for (const asset of S.coverQueue) S.coverPending.delete(asset.token);
  S.coverQueue = [];
  try {
    const result = await rpc('library', {
      game: $('game').value
    });
    if (epoch !== S.epoch) return;
    S.assets = result.assets;
    S.collections = result.collections;
    S.current = null;
    S.selected.clear();
    S.details = null;
    $('inspector').hidden = true;
    options('type', [...new Set(S.assets.map(a => a.type))].sort(), '全部类型');
    options('category', [...new Set(S.assets.map(a => a.category).filter(Boolean))].sort(), '全部分类');
    $('collection').replaceChildren(new Option('全部合集', ''), ...S.collections.map(c => new Option(c.name, c.id)));
    $('all-count').textContent = S.assets.length;
    $('fav-count').textContent = S.assets.filter(a => a.favorite).length;
    $('collection-count').textContent = S.collections.length;
    $('summary').textContent = `${S.assets.length.toLocaleString()} 个 Mod · ${S.assets.filter(a=>a.deployed).length.toLocaleString()} 已部署 · ${S.assets.filter(a=>a.favorite).length} 已收藏`;
    $('game-caption').textContent = $('game').selectedOptions[0]?.text + ' · AppID ' + $('game').value;
    $('viewport').scrollTop = 0;
    filter();
  } catch (error) {
    if (epoch === S.epoch) {
      S.assets = [];
      S.rows = [];
      $('grid').replaceChildren();
      $('results').textContent = '加载失败：' + error.message;
    }
  } finally {
    if (epoch === S.epoch) $('refresh').disabled = false;
  }
}

function filter() {
  const query = $('search').value.trim().toLocaleLowerCase(),
    status = $('status').value,
    type = $('type').value,
    category = $('category').value,
    collection = $('collection').value;
  const members = collection ? new Set(S.collections.find(c => c.id === collection)?.tokens || []) : null;
  const anyMembers = S.browse === 'collections' && !collection ? new Set(S.collections.flatMap(c => c.tokens)) : null;
  S.rows = S.assets.filter(a => (!query || (a.name + ' ' + a.workspace_id).toLocaleLowerCase().includes(query)) && (S.browse !== 'favorite' || a.favorite) && (!anyMembers || anyMembers.has(a.token)) && (!members || members.has(a.token)) && (!type || a.type === type) && (!category || a.category === category) && (status === 'all' || status === 'deployed' && a.deployed || status === 'undeployed' && !a.deployed || status === 'error' && a.status));
  const visible = new Set(S.rows.map(a => a.token));
  S.selected = new Set([...S.selected].filter(t => visible.has(t)));
  if (S.current && !visible.has(S.current.token)) closeInspector();
  $('results').textContent = `${S.rows.length.toLocaleString()} 个结果`;
  $('empty').hidden = !!S.rows.length;
  $('viewport').scrollTop = 0;
  selectionUI();
  reflow();
}

function selectionUI() {
  $('selection-tools').hidden = !S.selected.size;
  $('browse-hint').hidden = !!S.selected.size;
  $('selected-count').textContent = `${S.selected.size} 项已勾选`;
}

function reflow() {
  const viewport = $('viewport');
  const width = viewport.clientWidth - 8;
  const min = S.compact ? 166 : 186;
  const gap = 12;
  S.columns = Math.max(1, Math.floor((width + gap) / (min + gap)));
  S.width = Math.floor((width - (S.columns - 1) * gap) / S.columns);
  S.coverHeight = Math.round(S.width / 1.78);
  S.cell = S.coverHeight + 116 + gap;
  $('grid').style.height = Math.ceil(S.rows.length / S.columns) * S.cell + 'px';
  renderGrid();
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

function renderGrid() {
  const viewport = $('viewport'),
    first = Math.max(0, Math.floor(viewport.scrollTop / S.cell) - 1) * S.columns,
    last = Math.min(S.rows.length, (Math.ceil((viewport.scrollTop + viewport.clientHeight) / S.cell) + 1) * S.columns);
  const visible = new Set(S.rows.slice(first, last).map(a => a.token));
  if (S.current) visible.add(S.current.token);
  S.coverQueue = S.coverQueue.filter(a => {
    if (visible.has(a.token)) return true;
    S.coverPending.delete(a.token);
    return false;
  });
  const fragment = document.createDocumentFragment();
  for (let i = first; i < last; i++) {
    const a = S.rows[i],
      el = document.createElement('article');
    el.className = 'card' + (a.token === S.current?.token ? ' current' : '') + (S.selected.has(a.token) ? ' checked' : '');
    el.dataset.token = a.token;
    el.tabIndex = 0;
    el.setAttribute('aria-label', a.name + '，' + a.platform + '，' + (a.deployed ? '已部署' : '未部署'));
    el.style.cssText = `left:${i%S.columns*(S.width+12)}px;top:${Math.floor(i/S.columns)*S.cell}px;width:${S.width}px;height:${S.cell-12}px;--cover-height:${S.coverHeight}px`;
    el.innerHTML = `<div class="cover"><div data-cover="${a.token}" style="width:100%;height:100%;display:grid;place-items:center">${coverMarkup(a)}</div><input class="check" type="checkbox" ${S.selected.has(a.token)?'checked':''} aria-label="勾选 ${escape(a.name)}"></div><div class="card-body"><h3 class="card-title" title="${escape(a.name)}">${escape(a.name)}</h3><div class="card-line"><span class="platform">${escape(a.platform)}</span><span class="favorite" title="${a.favorite?'已收藏':'未收藏'}">${a.favorite?'★':'☆'}</span></div><div class="card-line"><span class="type-label" title="${escape(a.type+(a.category?' / '+a.category:''))}">${escape(a.type)}${a.category?' · '+escape(a.category):''}</span><span>${size(a.size)}</span></div><div class="card-line">${stateBadge(a)}${a.status?`<span class="badge warning" title="${escape(a.status)}">${escape(a.status)}</span>`:'<span>本地资产</span>'}</div></div>`;
    fragment.append(el);
    queueCover(a);
  }
  const focused = document.activeElement?.closest('.card')?.dataset.token;
  $('grid').replaceChildren(fragment);
  if (focused) $('grid').querySelector(`[data-token="${focused}"]`)?.focus({
    preventScroll: true
  });
}

function toggleSelection(token) {
  S.selected.has(token) ? S.selected.delete(token) : S.selected.add(token);
  selectionUI();
  renderGrid();
}
async function inspect(a) {
  const epoch = ++S.detailEpoch;
  S.current = a;
  S.details = null;
  const anchor = browseAnchor();
  $('inspector').hidden = false;
  drawInspector(a, null);
  reflow();
  restoreAnchor(anchor);
  try {
    const detail = await rpc('detail', {
      token: a.token
    });
    if (epoch !== S.detailEpoch || S.current?.token !== a.token) return;
    S.details = detail;
    drawInspector(a, detail);
  } catch (error) {
    if (epoch === S.detailEpoch) $('inspector-description').textContent = '详情读取失败：' + error.message;
  }
}

function drawInspector(a, d) {
  $('inspector').innerHTML = `<div class="inspector-top"><span>资产详情</span><button id="close-inspector" class="icon-button" aria-label="关闭详情侧栏">${icon('close')}</button></div><div class="inspector-scroll"><div class="inspector-overview"><div id="inspector-image">${coverMarkup(a,'inspector-cover')}</div><h2 class="inspector-name">${escape(a.name)}</h2></div><div class="badges">${stateBadge(a)}<span class="badge">${escape(a.platform)}</span>${a.favorite?'<span class="badge warning">已收藏</span>':''}</div><dl class="facts"><dt>类型 / 分类</dt><dd>${escape(a.type)}${a.category?' / '+escape(a.category):''}</dd><dt>本地大小</dt><dd>${size(a.size)}</dd><dt>Workspace ID</dt><dd>${escape(a.workspace_id||'未知')}</dd>${a.status?`<dt>资产状态</dt><dd>${escape(a.status)}</dd>`:''}</dl><h3 class="section-line">描述摘要</h3><div class="description inspector-summary" id="inspector-description">${d?d.html||'暂无描述。现有来源未提供内容。':'正在读取已验证的描述来源…'}</div>${d?`<p class="quiet">描述来源：${escape(d.source)}</p>`:''}</div><div class="inspector-footer"><button class="primary" id="open-detail" ${d?'':'disabled'}>阅读完整详情</button><span class="quiet">当前仅开放读取。收藏、合集、部署与外部打开不可用。</span></div>`;
  $('close-inspector').onclick = closeInspector;
  $('open-detail').onclick = openDetail;
  queueCover(a);
}

function closeInspector() {
  ++S.detailEpoch;
  S.current = null;
  S.details = null;
  const anchor = browseAnchor();
  $('inspector').hidden = true;
  reflow();
  restoreAnchor(anchor);
}

function openDetail() {
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
    ['类型 / 分类', a.type + (a.category ? ' / ' + a.category : '')],
    ['本地大小', size(a.size)],
    ['部署状态', a.deployed ? '已部署' : '未部署'],
    ['收藏', a.favorite ? '已收藏' : '未收藏'],
    ['Workspace ID', a.workspace_id || '未知']
  ];
  let content = tab === 'description' ? `<h3>完整描述</h3><div class="description">${d.html||'<p>暂无描述。现有本地来源未提供内容。</p>'}</div><p class="quiet section-line">描述来源：${escape(d.source)} · ${d.description.length.toLocaleString()} 字符 · 嵌入图片和外部资源不自动加载</p>` : tab === 'metadata' ? `<h3>身份与元数据</h3><dl class="metadata">${[['Mod 名称',a.name],['Internal UUID',d.internal_id],['Workspace ID',d.workspace_id],['SQLite PK',String(d.mod_pk)],['来源 URL',d.source_url||'未知'],['描述来源',d.source],['用户备注',d.notes||'暂无备注']].map(([k,v])=>`<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl>` : `<h3>文件与本地资源</h3><p class="quiet">显示现有记录，不执行目录扫描或部署操作。</p><h3>管理目录</h3><div class="resources">${escape(d.path||'未知')}</div><h3>文件记录</h3><div class="resources">${escape(formatFiles(d.files))}</div>`;
  $('detail-content').innerHTML = `<header class="detail-header"><div id="detail-image" data-token="${a.token}">${coverMarkup(a,'detail-cover')}</div><div class="detail-heading"><span class="eyebrow">ASSET DETAILS · ${escape($('game').selectedOptions[0]?.text)}</span><h2>${escape(a.name)}</h2><div class="badges">${stateBadge(a)}<span class="badge">${escape(a.platform)}</span><span class="badge">${escape(a.type)}</span></div></div><button class="icon-button" id="detail-close" aria-label="关闭完整详情">${icon('close')}</button></header><nav class="detail-tabs">${[['description','描述'],['metadata','身份与元数据'],['resources','文件与资源']].map(([key,label])=>`<button class="tab ${key===tab?'active':''}" data-detail-tab="${key}">${label}</button>`).join('')}</nav><div class="detail-scroll"><div class="detail-columns"><article class="detail-reading">${content}</article><aside class="detail-facts"><h3>资产摘要</h3><dl class="facts">${facts.map(([k,v])=>`<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl><p class="quiet section-line">安全只读<br>当前不提供业务写入或外部打开。</p></aside></div></div><footer class="detail-footer"><span>返回后保留查看对象、勾选集合与浏览位置</span><button id="detail-return">返回资产库</button></footer>`;
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
    renderGrid();
  });
}
$('viewport').addEventListener('scroll', scheduleGrid);
new ResizeObserver(() => reflow()).observe($('viewport'));
$('grid').addEventListener('click', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  const a = S.rows.find(a => a.token === card.dataset.token);
  if (e.target.matches('.check')) {
    toggleSelection(a.token);
    return;
  }
  inspect(a);
});
$('grid').addEventListener('dblclick', e => {
  const card = e.target.closest('.card');
  if (card?.dataset.token === S.current?.token && S.details) openDetail();
});
$('grid').addEventListener('keydown', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  const index = S.rows.findIndex(a => a.token === card.dataset.token);
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
for (const id of ['status', 'type', 'category', 'collection']) $(id).onchange = filter;
$('clear-search').onclick = () => {
  $('search').value = '';
  filter();
};
$('reset-filter').onclick = () => {
  $('search').value = '';
  $('status').value = 'all';
  $('type').value = '';
  $('category').value = '';
  $('collection').value = '';
  filter();
};
document.querySelectorAll('[data-browse]').forEach(button => button.onclick = () => {
  S.browse = button.dataset.browse;
  document.querySelectorAll('[data-browse]').forEach(el => el.classList.toggle('active', el === button));
  $('collection').hidden = S.browse !== 'collections';
  filter();
});
for (const id of ['standard', 'compact']) $(id).onclick = () => {
  const anchor = browseAnchor();
  S.compact = id === 'compact';
  $('compact').classList.toggle('active', S.compact);
  $('standard').classList.toggle('active', !S.compact);
  reflow();
  restoreAnchor(anchor);
};
$('refresh').onclick = load;
$('game').onchange = () => {
  $('search').value = '';
  $('status').value = 'all';
  $('type').value = '';
  $('category').value = '';
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
    $('game').replaceChildren(...result.games.map(g => new Option(g.name, g.key)));
    if (result.games.length) {
      $('game').value = (result.games.find(g => g.key !== '0') || result.games[0]).key;
      await load();
    } else {
      $('results').textContent = '数据库中没有游戏记录';
      $('summary').textContent = '0 个 Mod';
    }
  } catch (error) {
    $('results').textContent = '连接失败：' + error.message;
  }
});
