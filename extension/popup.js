const $ = (id) => document.getElementById(id);
const DEFAULTS = { minutes: 60, folder: 'claude-web-sync', knowledge: true };

function render(s) {
  if (!s) { $('status').textContent = '尚未同步過'; return; }
  const lines = [`${s.ok ? '成功' : '失敗'}:${s.msg}`, `時間:${new Date(s.time).toLocaleString()}`];
  if (s.email) lines.push(`帳號:${s.email}`);
  if (s.mode) lines.push(`讀取方式:${s.mode}`);
  if (s.changed?.length) lines.push('變更:\n  ' + s.changed.slice(0, 12).join('\n  ') + (s.changed.length > 12 ? '\n  …' : ''));
  if (s.problems?.length) lines.push('問題:\n  ' + s.problems.join('\n  '));
  $('status').textContent = lines.join('\n');
}

async function saveCfg() {
  const cfg = {
    minutes: Math.max(5, Number($('minutes').value) || 60),
    folder: ($('folder').value || DEFAULTS.folder).replace(/[\\:*?"<>|]/g, '_').replace(/^\/+|\/+$/g, '') || DEFAULTS.folder,
    knowledge: $('knowledge').checked,
  };
  await chrome.storage.local.set({ cfg });
  chrome.runtime.sendMessage({ type: 'reschedule' });
}

async function run(force) {
  $('status').textContent = '同步中…';
  render(await chrome.runtime.sendMessage({ type: 'sync', force }));
}

(async () => {
  const { cfg, status } = await chrome.storage.local.get(['cfg', 'status']);
  const c = { ...DEFAULTS, ...(cfg || {}) };
  $('minutes').value = c.minutes; $('folder').value = c.folder; $('knowledge').checked = c.knowledge;
  render(status);
  for (const id of ['minutes', 'folder', 'knowledge']) $(id).addEventListener('change', saveCfg);
  $('sync').addEventListener('click', async () => { await saveCfg(); run(false); });
  $('force').addEventListener('click', async () => { await saveCfg(); run(true); });
})();
