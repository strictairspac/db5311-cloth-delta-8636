import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {badge} from '../components/common/status.js';
import {statusLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

export function runsPage(state, identity) {
  const selected = state.runs.find(row=>row.id===identity) || state.runs[0];
  const cases = new Map(state.cases.map(row=>[row.id, row.title]));
  const rows = state.runs.map(row=>[`<a href="#runs/${row.id}">${esc(cases.get(row.case_id) || row.case_id)}</a>`, badge(row.status), esc(row.quota_version || '—'), esc(row.owner), esc(date(row.created_at))]);
  const result = state.evidence.find(row=>row.run_id===selected?.id);
  const active = selected && ['queued','running'].includes(selected.status);
  const quotaLabel = selected?.quota_version || '—';
  const detail = selected ? `<h2>${esc(cases.get(selected.case_id) || selected.case_id)}</h2>
    <p data-testid="run-status">${badge(selected.status)}</p>
    <dl><dt>配额版本</dt><dd data-testid="run-quota-version">${esc(quotaLabel)}</dd><dt>启动时间</dt><dd>${esc(date(selected.started_at))}</dd><dt>完成时间</dt><dd>${esc(date(selected.finished_at))}</dd></dl>
    ${selected.error ? `<p class="error">${esc(selected.error)}</p>` : ''}
    ${selected.status==='failed' && state.session.role==='operator' ? `<form id="retry-form"><button type="submit">按原配额版本重试</button></form><p class="note">重试沿用受理时冻结的配额版本。</p>` : ''}
    ${active?'<p class="note">后台处理中，记录会自动刷新。</p>':''}
    ${result?`<a class="button" href="#evidence/${result.id}">查看业务证据</a>`:''}` : '<h2>执行记录</h2><p>从档案创建一条执行记录后，可在此查看进度。</p>';
  const audit = state.audit.filter(x=>selected && x.reference===String(selected.id) && ['enqueue', 'retry', 'complete', 'failed'].includes(x.action));
  return `${heading(topic.modules[2], `${state.spec.entity_labels[1]}与后台处理进度`, '<button class="secondary" type="button" id="refresh">刷新记录</button>')}
    <div class="spread"><section>${records(['档案','状态','配额版本','经办','提交时间'], rows, '暂无执行记录。')}</section><aside class="brief">${detail}</aside></div>
    ${audit.length?panel('操作记录', records(['操作者','操作','时间'], audit.map(x=>[esc(x.actor),esc(statusLabels[x.action]||'提交执行'),esc(date(x.created_at))]))):''}`;
}

export function bindRuns(state, identity, api, update, fail) {
  document.getElementById('refresh').addEventListener('click', update);
  const selected = state.runs.find(row=>row.id===identity) || state.runs[0];
  if (!selected) return;
  submit('retry-form', async()=>{
    await api(`/runs/${selected.id}/retry`, {method:'POST'});
    await update();
  }, fail);
}
