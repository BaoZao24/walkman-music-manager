const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  settings: null,
  health: null,
  tracks: [],
  selected: new Map(),
  pendingByResult: new WeakMap(),
};

const pageNames = {
  home: '音乐助手',
  search: '搜索与下载',
  bilibili: 'Bilibili 翻唱',
  library: '曲库整理',
  settings: 'AI 与 API',
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

async function request(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`服务返回了无法读取的响应（HTTP ${response.status}）`);
  }
  if (!response.ok || data.ok === false) {
    const error = new Error(data.error || data.output || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(message, kind = 'success') {
  const node = document.createElement('div');
  node.className = `toast${kind === 'error' ? ' error' : ''}`;
  const mark = document.createElement('span');
  mark.className = 'toast-mark';
  mark.innerHTML = `<svg class="icon"><use href="#${kind === 'error' ? 'i-close' : 'i-check'}"></use></svg>`;
  const text = document.createElement('span');
  text.textContent = message;
  node.append(mark, text);
  $('#toast-region').append(node);
  window.setTimeout(() => node.remove(), 4200);
}

function navigate(page) {
  if (!pageNames[page]) return;
  $$('.page').forEach((section) => section.classList.toggle('active', section.id === `page-${page}`));
  $$('.nav-item[data-page]').forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  $('#current-page-title').textContent = pageNames[page];
  history.replaceState(null, '', `#${page}`);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function setLibraryTool(tool) {
  const valid = ['convert', 'repair', 'album'].includes(tool) ? tool : 'convert';
  $$('[data-tool-tab]').forEach((tab) => tab.classList.toggle('active', tab.dataset.toolTab === valid));
  $$('.tool-content').forEach((content) => content.classList.toggle('active', content.id === `tool-${valid}`));
}

function setBiliTool(tool) {
  const valid = ['list', 'fetch', 'download', 'organize'].includes(tool) ? tool : 'list';
  $$('[data-bili-tab]').forEach((tab) => tab.classList.toggle('active', tab.dataset.biliTab === valid));
  $$('.bili-content').forEach((content) => content.classList.toggle('active', content.id === `bili-${valid}`));
}

function setButtonBusy(button, busy, busyLabel = '处理中…') {
  if (!button) return;
  if (busy) {
    button.dataset.originalLabel = button.innerHTML;
    button.disabled = true;
    button.textContent = busyLabel;
  } else {
    button.disabled = false;
    if (button.dataset.originalLabel) button.innerHTML = button.dataset.originalLabel;
    delete button.dataset.originalLabel;
  }
}

function renderHealth(health) {
  state.health = health;
  const labels = {
    neteasecli: health.tools.neteasecli ? '可用' : '未安装',
    ncmdump: health.tools.ncmdump ? '可用' : '未安装',
    ffmpeg: health.tools.ffmpeg ? '可用' : '未安装',
    ai: health.ai_configured ? '已配置' : '待配置',
  };
  for (const [tool, label] of Object.entries(labels)) {
    const target = $(`[data-tool="${tool}"]`);
    if (!target) continue;
    target.textContent = label;
    target.closest('.status-card')?.classList.toggle('available', label === '可用' || label === '已配置');
    target.closest('.status-card')?.classList.toggle('unavailable', label === '未安装' || label === '待配置');
  }
  const configured = health.ai_configured;
  $('#connection-state').classList.toggle('connected', configured);
  $('#connection-state span').textContent = configured ? '已配置' : '未配置';
  updateHeroModel();
}

function updateHeroModel() {
  if (!state.settings) return;
  const chip = $('#hero-model');
  const configured = state.settings.ai_configured;
  chip.textContent = configured ? state.settings.model : '尚未配置';
  chip.title = configured ? state.settings.api_base_url : '请先设置 AI API';
}

function fillSettings(settings) {
  state.settings = settings;
  $('#setting-base-url').value = settings.api_base_url || '';
  $('#setting-model').value = settings.model || '';
  $('#setting-library-dir').value = settings.library_dir || '';
  $('#download-output').value = settings.library_dir || '';
  $$('.operation-form:not([data-action="bili_download"]) input[name="output_dir"]').forEach((input) => { if (!input.value) input.value = settings.library_dir || ''; });
  $$('input[name="root"]').forEach((input) => { if (!input.value) input.value = settings.library_dir || ''; });
  const biliOutput = $('.operation-form[data-action="bili_download"] [name="output_dir"]');
  if (biliOutput && !biliOutput.value) biliOutput.value = `${settings.library_dir.replace(/\/$/, '')}/Bilibili-Staging`;
  const biliDestination = $('.operation-form[data-action="bili_copy"] [name="dest"]');
  if (biliDestination && !biliDestination.value) biliDestination.value = settings.library_dir || '';
  const biliCookies = $('.operation-form[data-action="bili_list"] [name="cookies"]');
  if (biliCookies && !biliCookies.value) biliCookies.value = 'www.bilibili.com_cookies.txt';
  $('#clear-key-row').classList.toggle('hidden', !settings.api_key_configured);
  $('#setting-api-key').value = '';
  $('#setting-bilibili-cookies').value = settings.bilibili_cookies || '';
  $('#setting-api-key').disabled = false;
  $('#setting-api-key').placeholder = settings.api_key_configured ? '已保存 · 留空则保持不变' : '粘贴你的 API Key';
  $('#key-hint').textContent = settings.api_key_configured
    ? '已保存的密钥不会显示在页面中；留空即可保留当前密钥。'
    : '密钥只保存在本机配置文件中，不会回传到页面。';
  updateHeroModel();
}

async function loadAppState() {
  try {
    const [settings, health] = await Promise.all([request('/api/settings'), request('/api/health')]);
    fillSettings(settings);
    renderHealth(health);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function performSearch(query, limit = 20) {
  const summary = $('#search-summary');
  const results = $('#track-table-wrap');
  const button = $('button[type="submit"]', $('#search-form'));
  setButtonBusy(button, true, '正在搜索…');
  summary.textContent = '正在连接网易云音乐…';
  try {
    const response = await request('/api/search', { query, limit });
    const data = response.data || {};
    state.tracks = Array.isArray(data.tracks) ? data.tracks : [];
    summary.textContent = `“${query}” · 共 ${data.total ?? state.tracks.length} 首，显示 ${state.tracks.length} 首`;
    renderTracks();
  } catch (error) {
    state.tracks = [];
    results.innerHTML = `<div class="empty-state"><span class="empty-art"><svg class="icon"><use href="#i-close"></use></svg></span><strong>暂时无法完成搜索</strong><p>${escapeHtml(error.message)}</p></div>`;
    summary.textContent = '搜索遇到问题';
  } finally {
    setButtonBusy(button, false);
  }
}

function renderTracks() {
  const container = $('#track-table-wrap');
  if (!state.tracks.length) {
    container.innerHTML = '<div class="empty-state"><span class="empty-art"><svg class="icon"><use href="#i-music"></use></svg></span><strong>没有找到匹配的歌曲</strong><p>试试更短的歌名或歌手名称。</p></div>';
    updateDownloadPanel();
    return;
  }
  const rows = state.tracks.map((track, index) => {
    const id = String(track.id ?? '');
    const checked = state.selected.has(id) ? 'checked' : '';
    const artists = Array.isArray(track.artists) ? track.artists.join('、') : '';
    return `<tr>
      <td class="track-index">${String(index + 1).padStart(2, '0')}</td>
      <td class="track-select"><input type="checkbox" aria-label="选择 ${escapeHtml(track.name)}" data-track-id="${escapeHtml(id)}" ${checked}></td>
      <td class="track-name">${escapeHtml(track.name)}<span class="track-sub">${escapeHtml(artists || '未知艺术家')}</span></td>
      <td>${escapeHtml(track.album || '未知专辑')}</td>
      <td class="track-id">${escapeHtml(id)}</td>
      <td class="track-duration">${escapeHtml(track.durationFormatted || '—')}</td>
    </tr>`;
  }).join('');
  container.innerHTML = `<table class="track-table"><thead><tr><th>#</th><th></th><th>歌曲</th><th>专辑</th><th>歌曲 ID</th><th>时长</th></tr></thead><tbody>${rows}</tbody></table>`;
  updateDownloadPanel();
}

function updateDownloadPanel() {
  const panel = $('#download-panel');
  const count = state.selected.size;
  panel.classList.toggle('hidden', count === 0);
  $('#selected-count').textContent = `${count} 首`;
  if (!state.settings) return;
  if (!$('#download-output').value) $('#download-output').value = state.settings.library_dir;
}

$('#search-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const query = $('#search-query').value.trim();
  if (query) performSearch(query);
});

$('#track-table-wrap').addEventListener('change', (event) => {
  const checkbox = event.target.closest('[data-track-id]');
  if (!checkbox) return;
  const id = checkbox.dataset.trackId;
  const track = state.tracks.find((candidate) => String(candidate.id) === id);
  if (checkbox.checked && track) state.selected.set(id, track);
  else state.selected.delete(id);
  updateDownloadPanel();
});

$('#clear-selection').addEventListener('click', () => {
  state.selected.clear();
  renderTracks();
});

function getOperationResultBox(form) {
  return form.parentElement.querySelector('.operation-result');
}

function showOperationResult(box, result, { pending = false, applyLabel = '确认并执行' } = {}) {
  const output = result.output || result.error || '操作完成。';
  const heading = result.read_only ? '查询结果' : (pending ? '预览结果' : (result.ok ? '执行结果' : '操作未完成'));
  box.className = `operation-result${result.ok ? ' success' : ' error'}`;
  box.innerHTML = `
    <div class="result-heading"><span>${heading}</span><span class="result-status">${result.ok ? (pending ? '待确认' : '完成') : '请检查'}</span></div>
    <pre>${escapeHtml(output)}</pre>
    ${pending && result.ok ? `<div class="result-actions"><small>确认后才会执行文件写入或下载。</small><button class="button button-primary" type="button" data-confirm-operation>${escapeHtml(applyLabel)}<svg class="icon"><use href="#i-arrow"></use></svg></button></div>` : ''}`;
  box.classList.remove('hidden');
}

async function previewOperation(action, args, resultBox, applyLabel) {
  resultBox.className = 'operation-result';
  resultBox.innerHTML = '<div class="result-heading"><span>正在生成预览…</span><span class="result-status">处理中</span></div>';
  resultBox.classList.remove('hidden');
  state.pendingByResult.set(resultBox, { action, args, applyLabel });
  try {
    const result = await request('/api/action/preview', { action, args });
    showOperationResult(resultBox, result, { pending: result.ok && !result.read_only, applyLabel });
    if (!result.ok) state.pendingByResult.delete(resultBox);
  } catch (error) {
    showOperationResult(resultBox, { ok: false, error: error.message });
    state.pendingByResult.delete(resultBox);
  }
}

$$('.operation-form').forEach((form) => {
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const action = form.dataset.action;
    const args = {};
    for (const [key, value] of new FormData(form).entries()) args[key] = value;
    form.querySelectorAll('input[type="checkbox"][name]').forEach((checkbox) => { args[checkbox.name] = checkbox.checked; });
    const labels = { repair: '确认修复并备份', bili_download: '确认下载翻唱', bili_rename: '确认重命名', bili_copy: '确认复制入库' };
    previewOperation(action, args, getOperationResultBox(form), labels[action] || '确认并执行');
  });
});

$('#download-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (!state.selected.size) return toast('先从搜索结果中选择歌曲。', 'error');
  const args = {
    track_ids: [...state.selected.keys()],
    output_dir: $('#download-output').value.trim(),
    quality: $('#download-quality').value,
    lyrics: $('#download-lyrics').value,
  };
  previewOperation('download', args, $('#download-result'), '确认下载歌曲');
});

document.addEventListener('click', async (event) => {
  const confirmButton = event.target.closest('[data-confirm-operation]');
  if (!confirmButton) return;
  const box = confirmButton.closest('.operation-result');
  const pending = state.pendingByResult.get(box);
  if (!pending) return;
  const actionNames = { download: '下载歌曲', convert: '转换音乐文件', repair: '写入修复后的歌词（并创建备份）', album: '移动音频和歌词文件', bili_download: '下载翻唱音频', bili_rename: '重命名翻唱文件', bili_copy: '复制翻唱到音乐库' };
  const approved = window.confirm(`即将${actionNames[pending.action] || '执行此操作'}。\n\n请确认预览内容无误后继续。`);
  if (!approved) return;
  setButtonBusy(confirmButton, true, '正在执行…');
  try {
    const result = await request('/api/action/execute', { action: pending.action, args: pending.args });
    state.pendingByResult.delete(box);
    showOperationResult(box, result);
    if (result.ok) toast('任务已完成。');
  } catch (error) {
    state.pendingByResult.delete(box);
    showOperationResult(box, { ok: false, error: error.message });
  }
});

$('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('button[type="submit"]', event.currentTarget);
  const payload = {
    api_base_url: $('#setting-base-url').value.trim(),
    model: $('#setting-model').value.trim(),
    api_key: $('#setting-api-key').value,
    clear_api_key: $('#clear-api-key').checked,
    library_dir: $('#setting-library-dir').value.trim(),
    bilibili_cookies: $('#setting-bilibili-cookies').value.trim(),
  };
  setButtonBusy(button, true, '正在保存…');
  try {
    const result = await request('/api/settings', payload);
    fillSettings(result.settings);
    $('#clear-api-key').checked = false;
    const health = await request('/api/health');
    renderHealth(health);
    toast('设置已保存在本机。');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    setButtonBusy(button, false);
  }
});

$('#test-api').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  setButtonBusy(button, true, '正在连接…');
  try {
    const response = await request('/api/ai/test', {
      api_base_url: $('#setting-base-url').value.trim(),
      model: $('#setting-model').value.trim(),
      api_key: $('#setting-api-key').value,
      clear_api_key: $('#clear-api-key').checked,
    });
    $('#connection-state').classList.add('connected');
    $('#connection-state span').textContent = '测试通过';
    toast(`AI 接口测试通过：${response.message}。保存设置后即可用于规划。`);
  } catch (error) {
    $('#connection-state').classList.remove('connected');
    $('#connection-state span').textContent = '连接失败';
    toast(error.message, 'error');
  } finally {
    setButtonBusy(button, false);
  }
});

$('#clear-api-key').addEventListener('change', (event) => {
  const input = $('#setting-api-key');
  input.disabled = event.target.checked;
  input.value = '';
});

document.addEventListener('click', (event) => {
  const pageTrigger = event.target.closest('[data-page]');
  if (pageTrigger) {
    if (pageTrigger.tagName === 'A') event.preventDefault();
    navigate(pageTrigger.dataset.page);
    const tool = pageTrigger.dataset.libraryTarget;
    if (tool) setLibraryTool(tool);
    return;
  }
  const tab = event.target.closest('[data-tool-tab]');
  if (tab) setLibraryTool(tab.dataset.toolTab);
  const biliTab = event.target.closest('[data-bili-tab]');
  if (biliTab) setBiliTool(biliTab.dataset.biliTab);
});

document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    navigate('search');
    $('#search-query').focus();
  }
  if (event.key === 'Escape' && location.hash && location.hash !== '#home') navigate('home');
});

const dateLabel = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' }).format(new Date());
$('.welcome-row .eyebrow').textContent = dateLabel;

const firstPage = location.hash.slice(1);
if (pageNames[firstPage]) navigate(firstPage);
loadAppState();
