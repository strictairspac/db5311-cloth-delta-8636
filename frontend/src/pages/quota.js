import {heading} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';
import {topic} from '../constants/project.js';

function quotaRows(quotas) {
  return (quotas || []).map(row=>`${esc(row.grade)} ${esc(row.capacity_g)} 克 / ${esc(row.capacity_pieces)} 件`).join('<br>');
}

export function quotaPage(state) {
  const versions = state.quotaVersions || [];
  const rows = versions.map(row=>[
    esc(`v${row.version}`), esc(row.effective_date), quotaRows(row.quotas), esc(row.author), esc(date(row.created_at)),
  ]);
  const canPlan = state.session.role==='reviewer';
  const latest = versions.map(row=>row.quotas).find(Boolean) || state.spec.quota_sample;
  const form = canPlan ? `<form id="quota-form" class="field-grid">
    <label class="field"><span>生效日</span><input name="effective_date" type="date" value="${esc(new Date().toISOString().slice(0,10))}" required /></label>
    <label class="field data"><span>整套分级仓位配额</span><small>每条含等级、重量上限（克）、件数上限</small>
      <textarea name="quotas" rows="9" spellcheck="false">${esc(JSON.stringify(latest, null, 2))}</textarea></label>
    <p class="note">新版本按生效日生效。切换失败时旧版本继续生效；核算按核算日选择当时生效的一版，并冻结在配额单上。</p>
    <div class="actions form-actions"><button>排定配额版本</button></div></form>` : '';

  const latestText = versions.length ? `当前最新版本 v${versions[0].version}，自 ${esc(versions[0].effective_date)} 起生效。` : '';
  return `${heading(topic.modules[4], '提前排好各等级的整套配额。核算时按核算日选用当时生效的一版，并冻结在配额单上。')}
    <div class="spread"><section aria-label="配额版本">${records(['版本','生效日','整套配额','排定人','排定时间'], rows, '尚未排定配额版本。')}</section>
    <aside class="brief"><h2>排定新版本</h2>${latestText ? `<p class="note">${latestText}</p>` : ''}${form || '<p class="note">库管复核员可以排定配额版本。</p>'}</aside></div>`;
}

export function bindQuota(api, update, fail) {
  submit('quota-form', async data=>{
    let quotas;
    try { quotas = JSON.parse(data.get('quotas')); }
    catch { throw new Error('请检查整套配额的 JSON 格式。'); }
    await api('/quota-versions', {method:'POST', data:{effective_date:data.get('effective_date'), quotas}});
    await update();
  }, fail);
}
