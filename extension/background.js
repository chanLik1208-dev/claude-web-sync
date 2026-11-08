// Claude Web Sync — MV3 service worker.
// 在「你自己已登入的 Chrome」裡定時讀 claude.ai 的內部端點,存成檔案到 Downloads/<folder>/。
// 端點為未公開介面,可能隨時改版;每個項目獨立容錯。
const BASE = 'https://claude.ai';
const DEFAULTS = { minutes: 60, folder: 'claude-web-sync', knowledge: true, port: 47821 };
const MEMORY_CANDIDATES = [
  (o) => `/api/organizations/${o}/memory`,
  (o) => `/api/organizations/${o}/memory/synthesis`,
];
const STYLE_CANDIDATES = [
  (o) => `/api/organizations/${o}/list_styles`,
  (o) => `/api/organizations/${o}/styles`,
];

// ───────── 小工具 ─────────
const sortKeys = (o) =>
  Array.isArray(o) ? o.map(sortKeys)
    : o && typeof o === 'object' ? Object.fromEntries(Object.keys(o).sort().map((k) => [k, sortKeys(o[k])]))
      : o;
const json = (o) => JSON.stringify(sortKeys(o), null, 2);
const slug = (s, f = 'untitled') => {
  const x = String(s ?? '').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').trim().replace(/^[. ]+|[. ]+$/g, '').slice(0, 80);
  return x || f;
};
const asList = (d, ...keys) =>
  Array.isArray(d) ? d : d && typeof d === 'object' ? keys.map((k) => d[k]).find(Array.isArray) || [] : [];
function findStrings(obj, re, path = '') {
  const out = [];
  if (Array.isArray(obj)) obj.forEach((v, i) => out.push(...findStrings(v, re, `${path}[${i}]`)));
  else if (obj && typeof obj === 'object') {
    for (const [k, v] of Object.entries(obj)) {
      const p = path ? `${path}.${k}` : k;
      if (typeof v === 'string' && v.trim() && re.test(k)) out.push([p, v]);
      else out.push(...findStrings(v, re, p));
    }
  }
  return out;
}
function b64(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
const getCfg = async () => ({ ...DEFAULTS, ...((await chrome.storage.local.get('cfg')).cfg || {}) });

// ───────── API:先用 service worker fetch;被拒就改走 claude.ai 分頁 ─────────
function makeApi() {
  const st = { useTab: false, tabId: null, created: false };

  async function viaFetch(path, body) {
    const init = { credentials: 'include', headers: { accept: 'application/json' } };
    if (body !== undefined) {
      init.method = 'POST';
      init.headers['content-type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const r = await fetch(BASE + path, init);
    const t = await r.text();
    let j = null;
    try { j = JSON.parse(t); } catch { /* 非 JSON */ }
    return { s: r.status, j, t };
  }

  async function ensureTab() {
    if (st.tabId != null) return st.tabId;
    const [existing] = await chrome.tabs.query({ url: `${BASE}/*` });
    if (existing) { st.tabId = existing.id; return st.tabId; }
    const tab = await chrome.tabs.create({ url: `${BASE}/settings/profile`, active: false });
    st.tabId = tab.id; st.created = true;
    await new Promise((res) => {
      const done = (id, info) => { if (id === tab.id && info.status === 'complete') { chrome.tabs.onUpdated.removeListener(done); res(); } };
      chrome.tabs.onUpdated.addListener(done);
      setTimeout(res, 30000);
    });
    return st.tabId;
  }

  async function viaTab(path, body) {
    const tabId = await ensureTab();
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId }, world: 'MAIN', args: [path, body === undefined ? null : body],
      func: async (p, b) => {
        const init = { credentials: 'include', headers: { accept: 'application/json' } };
        if (b !== null) { init.method = 'POST'; init.headers['content-type'] = 'application/json'; init.body = JSON.stringify(b); }
        try {
          const r = await fetch(p, init); const t = await r.text(); let j = null;
          try { j = JSON.parse(t); } catch { /* 非 JSON */ }
          return { s: r.status, j, t };
        } catch { return { s: 0, j: null, t: '' }; }
      },
    });
    return result;
  }

  const call = async (path, body) => {
    if (!st.useTab) {
      try {
        const r = await viaFetch(path, body);
        if (![0, 401, 403].includes(r.s)) return r;
      } catch { /* 改走分頁 */ }
      st.useTab = true;
    }
    return viaTab(path, body);
  };
  return {
    get: (p) => call(p),
    post: (p, b) => call(p, b),
    mode: () => (st.useTab ? 'tab' : 'fetch'),
    cleanup: async () => { if (st.created && st.tabId != null) { try { await chrome.tabs.remove(st.tabId); } catch { /* 已關 */ } } },
  };
}

// ───────── 記憶本文(melange):讀取端點未公開,依序試;成功的會記住 ─────────
// 已由網頁實際請求確認:POST /melange/read,body 是 {"path": "/topics/xxx.md"}(不是 memory_id)
const MEL_READS = [
  (o, it) => ['POST', `/api/organizations/${o}/melange/read`, { path: it.path }],
];
function pickBody(r) {
  if (r.s !== 200) return null;
  if (r.j) {
    const known = findStrings(r.j, /^(content|text|body|markdown|memory)$/).map(([, v]) => v).join('\n').trim();
    if (known) return known;
    // 回應欄位名稱未確認:退而取 JSON 裡最長的字串
    const all = findStrings(r.j, /./).map(([, v]) => v).sort((a, b) => b.length - a.length);
    return all[0]?.trim() || null;
  }
  const t = (r.t || '').trim();
  return t && !t.startsWith('<') ? t : null; // 擋掉 200 但其實是 HTML 的情況
}
async function readMelange(api, oid, it) {
  const saved = (await chrome.storage.local.get('melRead')).melRead;
  const order = saved != null ? [saved] : MEL_READS.map((_, i) => i);
  for (const i of order) {
    const [method, path, body] = MEL_READS[i](oid, it);
    const r = method === 'GET' ? await api.get(path) : await api.post(path, body);
    const text = pickBody(r);
    if (text !== null) { await chrome.storage.local.set({ melRead: i }); return text; }
  }
  if (saved != null) await chrome.storage.local.remove('melRead'); // 記住的失效了,下次重新試
  return null;
}

// 唯讀偵測:記下「你在網頁上」對 melange 發出的請求(只存 method/url/前 300 字 body,最多 30 筆)
chrome.webRequest.onBeforeRequest.addListener((d) => {
  if (d.tabId < 0) return; // 排除擴充功能自己的請求
  let body = '';
  try { body = d.requestBody?.raw?.[0]?.bytes ? new TextDecoder().decode(d.requestBody.raw[0].bytes).slice(0, 300) : ''; } catch { /* 忽略 */ }
  chrome.storage.local.get('melLog').then(({ melLog = [] }) => {
    melLog.push({ t: Date.now(), method: d.method, url: d.url.replace(BASE, ''), body });
    chrome.storage.local.set({ melLog: melLog.slice(-30) });
  });
}, { urls: ['https://claude.ai/api/*melange*'] }, ['requestBody']);

// ───────── 收集 ─────────
async function collect(api, cfg) {
  const out = {}; const problems = [];
  const put = (path, text, history = true) => { out[path] = { text: text.endsWith('\n') ? text : text + '\n', history }; };

  const orgs = await api.get('/api/organizations');
  if (orgs.s !== 200 || !Array.isArray(orgs.j)) throw new Error(`未登入或被擋 (HTTP ${orgs.s})。請先在這個 Chrome 登入 claude.ai`);
  put('raw/organizations.json', json(orgs.j), false);
  const org = orgs.j.find((o) => (o.capabilities || []).includes('chat')) || orgs.j[0];
  const oid = org.uuid;

  const acc = await api.get('/api/account');
  let email = null;
  if (acc.s === 200 && acc.j) { email = acc.j.email_address; put('raw/account.json', json(acc.j), false); }
  else problems.push(`account: HTTP ${acc.s}`);

  const prof = await api.get('/api/account_profile');
  if (prof.s === 200 && prof.j) {
    put('raw/account_profile.json', json(prof.j), false);
    const body = ['conversation_preferences', 'cowork_global_instructions']
      .filter((k) => typeof prof.j[k] === 'string' && prof.j[k].trim())
      .map((k) => `## ${k}\n\n${prof.j[k]}`).join('\n\n');
    put('preferences.md', body || '(沒有自訂指令)');
  } else problems.push(`account_profile: HTTP ${prof.s}`);

  const ms = await api.get(`/api/organizations/${oid}/memory/settings`);
  if (ms.s === 200) put('raw/memory_settings.json', json(ms.j), false);
  const sections = [];
  let memOk = false;
  for (const f of MEMORY_CANDIDATES) {
    const m = await api.get(f(oid));
    if (m.s === 200) {
      put('raw/memory.json', json(m.j), false); memOk = true;
      if (m.j && typeof m.j.memory === 'string' && m.j.memory.trim()) sections.push(`## memory\n\n${m.j.memory.trim()}`);
      break;
    }
  }
  if (!memOk) problems.push('memory: 候選端點都失敗');
  const mel = await api.post(`/api/organizations/${oid}/melange/list`, {});
  if (mel.s === 200 && mel.j) {
    put('raw/memory_melange.json', json(mel.j), false);
    const items = asList(mel.j, 'data');
    let readFail = 0;
    for (const it of items) {
      const title = (it.display_path_segments || []).join(' / ') || it.display_name || it.path || it.memory_id;
      const body = await readMelange(api, oid, it);
      if (body === null) readFail++;
      sections.push(`## ${title}\n(${it.path || ''},更新:${it.updated_at || '?'})\n\n${it.description || ''}${body ? `\n\n${body}` : ''}`);
    }
    if (items.length && readFail === items.length) {
      problems.push('memory: 目錄抓到了但讀不到本文(端點未知)。請在網頁點開一筆記憶,再看 popup 的「偵測到的記憶請求」');
    }
  }
  put('memory.md', sections.join('\n\n') || `(目前沒有記憶內容;記憶模式:${ms.j?.memory_mode ?? '未知'})`);

  for (const f of STYLE_CANDIDATES) {
    const s = await api.get(f(oid));
    if (s.s === 200) {
      put('raw/styles.json', json(s.j), false);
      for (const st of asList(s.j, 'styles', 'custom_styles', 'data')) {
        if (st && (st.type ?? 'custom') === 'custom') put(`styles/${slug(st.name)}.md`, st.prompt || JSON.stringify(st, null, 2));
      }
      break;
    }
  }

  let pr = await api.get(`/api/organizations/${oid}/projects?limit=200`);
  if (pr.s !== 200) pr = await api.get(`/api/organizations/${oid}/projects`);
  if (pr.s === 200) {
    put('raw/projects.json', json(pr.j), false);
    for (const p of asList(pr.j, 'projects', 'data')) {
      const base = `projects/${slug(p.name)}-${String(p.uuid).slice(0, 8)}`;
      put(`${base}/instructions.md`, (p.prompt_template || '').trim() || '(無自訂指令)');
      put(`${base}/README.md`, `# ${p.name}\n\n${p.description || ''}`.trimEnd());
      if (!cfg.knowledge) continue;
      const d = await api.get(`/api/organizations/${oid}/projects/${p.uuid}/docs`);
      if (d.s === 200) for (const doc of asList(d.j, 'docs', 'data')) put(`${base}/knowledge/${slug(doc.file_name || doc.uuid)}`, doc.content || '');
      else problems.push(`project ${p.name} docs: HTTP ${d.s}`);
    }
  } else problems.push(`projects: HTTP ${pr.s}`);

  return { out, problems, email, org: org.name };
}

// ───────── 寫檔(Downloads/<folder>/…,只在內容變動時寫;舊版進 history/) ─────────
async function save(filename, text) {
  const id = await chrome.downloads.download({
    // 不能用 text/plain:Chrome 會依 MIME 把 .md/.json 改成 .txt。octet-stream 不會動副檔名
    url: `data:application/octet-stream;base64,${b64(text)}`, filename, conflictAction: 'overwrite', saveAs: false,
  });
  for (let i = 0; i < 100; i++) {
    const [d] = await chrome.downloads.search({ id });
    if (d?.state === 'complete') return;
    if (d?.state === 'interrupted') throw new Error(`下載中斷:${filename} (${d.error})`);
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`下載逾時:${filename}`);
}

async function writeAll(out, cfg, force) {
  const snap = (await chrome.storage.local.get('snap')).snap || {};
  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  const changed = [];
  for (const [path, { text, history }] of Object.entries(out)) {
    const old = snap[path];
    if (!force && old === text) continue;
    if (old !== undefined && old !== text && history) await save(`${cfg.folder}/history/${path}.${stamp}`, old);
    await save(`${cfg.folder}/${path}`, text);
    snap[path] = text; changed.push(path);
  }
  await chrome.storage.local.set({ snap });
  await save(`${cfg.folder}/last_sync.txt`, new Date().toISOString());
  return changed;
}

// ───────── 主流程 ─────────
let running = false;
async function runSync({ force = false } = {}) {
  if (running) return { ok: false, msg: '同步進行中' };
  running = true;
  const api = makeApi();
  let status;
  try {
    const cfg = await getCfg();
    const { out, problems, email, org } = await collect(api, cfg);
    const changed = await writeAll(out, cfg, force);
    status = { ok: true, time: Date.now(), email, org, mode: api.mode(), changed, problems, msg: `完成:${changed.length} 個檔案有變更` };
  } catch (e) {
    status = { ok: false, time: Date.now(), msg: String(e.message || e) };
  } finally {
    await api.cleanup();
    running = false;
  }
  await chrome.storage.local.set({ status });
  return status;
}

// ───────── 橋接:問本機 MCP server 有沒有人要同步(long-poll,每 30 秒一輪) ─────────
let polling = false;
async function pollBridge() {
  if (polling) return;
  polling = true;
  try {
    const { port } = await getCfg();
    const base = `http://127.0.0.1:${Number(port) || DEFAULTS.port}`;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 27000);
    let req;
    try {
      const r = await fetch(`${base}/poll`, { headers: { 'X-CWS': '1' }, signal: ctl.signal });
      if (!r.ok) return;
      req = await r.json();
    } finally { clearTimeout(timer); }
    if (!req?.id) return;
    const status = await runSync({ force: !!req.force });
    await fetch(`${base}/result`, {
      method: 'POST', headers: { 'X-CWS': '1', 'content-type': 'application/json' },
      body: JSON.stringify({ id: req.id, status }),
    });
  } catch { /* MCP server 沒開:靜默 */ } finally { polling = false; }
}

async function schedule() {
  const { minutes } = await getCfg();
  await chrome.alarms.create('sync', { delayInMinutes: 1, periodInMinutes: Math.max(5, Number(minutes) || 60) });
  await chrome.alarms.create('poll', { delayInMinutes: 0.1, periodInMinutes: 0.5 });
}
chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === 'sync') runSync();
  else if (a.name === 'poll') pollBridge();
});
chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg?.type === 'sync') { runSync({ force: !!msg.force }).then(reply); return true; }
  if (msg?.type === 'reschedule') { schedule().then(() => reply(true)); return true; }
  return false;
});
