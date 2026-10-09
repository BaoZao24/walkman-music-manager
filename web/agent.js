/* The conversation drives tools; forms are optional manual controls. */
const agentState = { sessionId: null, activeJob: null, polling: false };
const agentConversation = $('#agent-conversation');
const agentForm = $('#ai-prompt-form');
const agentInput = $('#ai-prompt');
const agentSubmit = $('button[type="submit"]', agentForm);
const agentCancel = $('#cancel-task');
const tokenNumber = (value) => Number(value || 0).toLocaleString('zh-CN');

function tokenSummary(usage) {
  const incomplete = usage.unreported_requests || usage.partial_requests;
  return (incomplete ? '≥ ' : '') + tokenNumber(usage.total_tokens);
}

function tokenDetails(usage) {
  const parts = ['输入 ' + tokenNumber(usage.input_tokens), '输出 ' + tokenNumber(usage.output_tokens), tokenNumber(usage.requests) + ' 次请求'];
  if (usage.cached_tokens) parts.push('缓存 ' + tokenNumber(usage.cached_tokens));
  if (usage.reasoning_tokens) parts.push('推理 ' + tokenNumber(usage.reasoning_tokens));
  if (usage.unreported_requests) parts.push(tokenNumber(usage.unreported_requests) + ' 次未返回用量');
  if (usage.partial_requests) parts.push(tokenNumber(usage.partial_requests) + ' 次仅返回部分用量');
  return parts.join(' · ');
}

function renderUsageTotals(usage) {
  if (!usage) return;
  $('#usage-today').textContent = tokenSummary(usage.today);
  $('#usage-total').textContent = tokenSummary(usage.total);
  const breakdown = $('#usage-breakdown');
  breakdown.replaceChildren(agentNode('p', '', '今日：' + tokenDetails(usage.today)), agentNode('p', '', '累计：' + tokenDetails(usage.total)));
  for (const [model, counts] of Object.entries(usage.models || {})) {
    breakdown.append(agentNode('p', 'usage-model', model + '：' + tokenSummary(counts) + ' token · ' + tokenDetails(counts)));
  }
  if (usage.sources?.lyrics?.requests) {
    breakdown.append(agentNode('p', '', '其中歌词翻译：' + tokenSummary(usage.sources.lyrics) + ' token · ' + tokenDetails(usage.sources.lyrics)));
  }
  if (usage.since) breakdown.append(agentNode('p', 'usage-note', '开始记录：' + new Date(usage.since).toLocaleString('zh-CN')));
  const warning = $('#usage-warning');
  warning.textContent = usage.storage_error || (usage.total.unreported_requests || usage.total.partial_requests ? '部分请求没有完整用量，显示的是已报告的 token 合计。' : '');
  warning.classList.toggle('hidden', !warning.textContent);
}

async function refreshUsage() {
  try {
    const response = await request('/api/usage');
    renderUsageTotals(response.usage);
  } catch (error) {
    $('#usage-warning').textContent = '暂时无法读取用量：' + error.message;
    $('#usage-warning').classList.remove('hidden');
  }
}

function agentNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setAgentBusy(busy) {
  setButtonBusy(agentSubmit, busy, '正在执行…');
  $('#new-conversation').disabled = busy;
  agentCancel.classList.toggle('hidden', !busy);
  agentCancel.disabled = false;
  agentCancel.textContent = '停止';
  $$('[data-prompt]').forEach((button) => { button.disabled = busy; });
  agentConversation.setAttribute('aria-busy', String(busy));
}

function newAgentTurn(prompt) {
  $('#agent-empty')?.remove();
  $('.agent-new-hint')?.remove();
  const user = agentNode('div', 'agent-message agent-user');
  user.append(agentNode('span', 'agent-role', '你'), agentNode('p', '', prompt));
  const response = agentNode('div', 'agent-response');
  const status = agentNode('div', 'agent-task-status', '正在连接 AI…');
  const steps = agentNode('div', 'agent-steps');
  const answer = agentNode('div', 'agent-answer');
  const usage = agentNode('div', 'agent-turn-usage hidden');
  response.append(agentNode('span', 'agent-role', 'WALKMAN'), status, steps, answer, usage);
  agentConversation.append(user, response);
  agentConversation.scrollTop = agentConversation.scrollHeight;
  return { response, status, steps, answer, usage, tools: new Map(), nextSeq: 0 };
}

function renderAgentJob(job, turn) {
  const shouldScroll = agentConversation.scrollHeight - agentConversation.scrollTop - agentConversation.clientHeight < 120;
  if (job.usage && job.usage.requests) {
    turn.usage.textContent = '本次 ' + tokenSummary(job.usage) + ' token · ' + tokenDetails(job.usage);
    turn.usage.classList.remove('hidden');
  }
  renderUsageTotals(job.usage_totals);
  for (const event of job.events || []) {
    if (event.seq < turn.nextSeq) continue;
    turn.nextSeq = event.seq + 1;
    if (event.type === 'thinking') {
      turn.status.textContent = event.message;
    } else if (event.type === 'tool_started') {
      const card = agentNode('div', 'agent-step running');
      const header = agentNode('div', 'agent-step-heading');
      const mark = agentNode('span', 'agent-step-mark', '•');
      const label = agentNode('strong', '', event.label);
      const stateLabel = agentNode('span', 'agent-step-state', '执行中');
      header.append(mark, label, stateLabel);
      const details = agentNode('details', 'agent-step-details');
      const output = agentNode('pre', 'agent-step-output');
      details.append(agentNode('summary', '', '查看输出'), output);
      details.classList.add('hidden');
      card.append(header, details);
      turn.steps.append(card);
      turn.tools.set(event.call_id, { card, mark, stateLabel, details, output });
      turn.status.textContent = event.label + '…';
    } else if (event.type === 'tool_output') {
      const tool = turn.tools.get(event.call_id);
      if (tool) {
        tool.output.textContent = (tool.output.textContent + '\n' + event.output).trim().slice(-8000);
        tool.details.classList.remove('hidden');
      }
    } else if (event.type === 'tool_finished') {
      const tool = turn.tools.get(event.call_id);
      if (tool) {
        tool.card.classList.remove('running');
        tool.card.classList.add(event.ok ? 'completed' : 'failed');
        tool.mark.textContent = event.ok ? '✓' : '!';
        tool.stateLabel.textContent = event.ok ? '完成' : '未完成';
        if (event.output) {
          tool.output.textContent = event.output;
          tool.details.classList.remove('hidden');
        }
      }
    } else if (event.type === 'assistant' || event.type === 'cancelled') {
      turn.answer.textContent = event.message;
    }
  }
  if (job.status === 'cancelling') turn.status.textContent = '正在停止当前步骤…';
  if (['completed', 'needs_input', 'failed', 'cancelled'].includes(job.status)) {
    turn.response.classList.add('finished');
    const labels = { completed: '任务结束', needs_input: '需要你补充信息', failed: '任务暂停', cancelled: '已停止' };
    turn.status.textContent = labels[job.status];
    turn.answer.textContent = job.error || job.result || turn.answer.textContent;
    turn.answer.classList.toggle('agent-error', job.status === 'failed');
  }
  if (shouldScroll) agentConversation.scrollTop = agentConversation.scrollHeight;
}

async function pollAgentJob(job, turn) {
  let errors = 0;
  while (agentState.activeJob === job.id) {
    try {
      const response = await request('/api/ai/jobs/' + job.id + '?after=' + turn.nextSeq);
      errors = 0;
      renderAgentJob(response.job, turn);
      if (['completed', 'needs_input', 'failed', 'cancelled'].includes(response.job.status)) return;
    } catch (error) {
      errors += 1;
      turn.status.textContent = '进度连接中断，正在重新连接…';
      if (error.status === 404 || errors >= 10) {
        turn.answer.textContent = '无法连接本机任务服务：' + error.message + '。请重新打开客户端后继续。';
        turn.answer.classList.add('agent-error');
        agentState.sessionId = null;
        return;
      }
    }
    await new Promise((resolve) => window.setTimeout(resolve, errors ? 2000 : 650));
  }
}

agentForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (agentState.polling) return;
  const prompt = agentInput.value.trim();
  if (!prompt) return;
  if (state.settings && !state.settings.ai_configured) {
    navigate('settings');
    toast('请先连接支持工具调用的 AI 服务。', 'error');
    return;
  }
  const turn = newAgentTurn(prompt);
  agentState.polling = true;
  setAgentBusy(true);
  agentCancel.disabled = true;
  agentInput.value = '';
  agentInput.style.height = '';
  try {
    const response = await request('/api/ai/run', { prompt, session_id: agentState.sessionId });
    agentState.sessionId = response.job.session_id;
    agentState.activeJob = response.job.id;
    agentCancel.disabled = false;
    renderAgentJob(response.job, turn);
    await pollAgentJob(response.job, turn);
  } catch (error) {
    turn.status.textContent = '无法启动任务';
    turn.answer.textContent = error.message;
    turn.answer.classList.add('agent-error');
    agentInput.value = prompt;
    if (error.message.includes('会话已结束')) agentState.sessionId = null;
  } finally {
    agentState.activeJob = null;
    agentState.polling = false;
    setAgentBusy(false);
    $('span', agentSubmit).textContent = agentState.sessionId ? '继续执行' : '开始执行';
    agentInput.focus();
    refreshUsage();
  }
});

agentCancel.addEventListener('click', async () => {
  if (!agentState.activeJob) return;
  agentCancel.disabled = true;
  agentCancel.textContent = '正在停止…';
  try {
    await request('/api/ai/cancel', { job_id: agentState.activeJob });
  } catch (error) {
    toast(error.message, 'error');
    agentCancel.disabled = false;
    agentCancel.textContent = '停止';
  }
});

agentInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!agentState.polling) agentForm.requestSubmit();
  }
});
agentInput.addEventListener('input', () => {
  agentInput.style.height = 'auto';
  agentInput.style.height = Math.min(200, agentInput.scrollHeight) + 'px';
});

$('#new-conversation').addEventListener('click', () => {
  if (agentState.polling) return;
  agentState.sessionId = null;
  agentConversation.replaceChildren(agentNode('p', 'agent-new-hint', '新的任务，从一句话开始。'));
  $('span', agentSubmit).textContent = '开始执行';
  agentInput.value = '';
  agentInput.focus();
});
$$('[data-prompt]').forEach((button) => {
  button.addEventListener('click', () => {
    if (agentState.polling) return;
    agentInput.value = button.dataset.prompt;
    agentForm.requestSubmit();
  });
});

request('/api/ai/tools').then((response) => {
  for (const tool of response.tools || []) {
    $('#agent-tools').append(agentNode('span', 'agent-tool-chip', tool.label));
  }
}).catch((error) => { $('#agent-tools').textContent = error.message; });

refreshUsage();
window.setInterval(() => { if (!document.hidden && !agentState.polling) refreshUsage(); }, 15000);
