import { api } from '../api.js';
import { ENUMS } from '../config.js';
import { barList, emptyState, errorBox, errorMessage, esc, fmtPct, fmtTime, ladder, readouts, waterfall } from '../ui.js';

function editionBadge(run) {
  if (!run) return '';
  if (run.is_active) return '<span class="badge ok">正式版</span>';
  return run.stage === 'done' ? '<span class="badge warn">候选版·待切换</span>' : '<span class="badge info">候选版·挖掘中</span>';
}

function runBlock(run) {
  if (!run) return emptyState({ title: '还没有挖掘记录', hint: '去「模式挖掘」发起一次 PrefixSpan。' });
  return `<dl class="kv">
    <dt>版本</dt><dd>${editionBadge(run)} <span class="mono">${esc(run.run_no)}</span></dd>
    <dt>阶段</dt><dd>${esc(ENUMS.runStage[run.stage] || run.stage)} · ${esc(ENUMS.mineStatus[run.status] || run.status)}</dd>
    <dt>本批水位</dt><dd class="mono">${fmtTime(run.watermark_at)} · 已并入 ${esc(run.ingested_events)} 事件</dd>
    <dt>开始</dt><dd>${fmtTime(run.started_at)}</dd>
    <dt>模式</dt><dd>${esc(run.pattern_count)} 条 · top 支持度 ${fmtPct(run.top_support_pct)}</dd>
    <dt>推荐</dt><dd>${esc(run.rec_count)} 条 · 覆盖 ${esc(run.covered_users)}/${esc(run.active_users)} 用户（${fmtPct(run.coverage_pct)}）</dd>
    <dt>复核继承</dt><dd>沿用 ${esc(run.carried_recs)} · 变化回待复核 ${esc(run.reset_recs)}</dd>
  </dl>`;
}

function diffBlock(diff) {
  if (!diff) return '<div class="legend"><span>暂无待切换候选版。</span></div>';
  const p = diff.patterns;
  const r = diff.recommendations;
  return `<div style="display:grid;gap:8px">
    <div class="legend">
      <span>切换：${esc(diff.base ? diff.base.run_no : '无正式版')} → <b>${esc(diff.target.run_no)}</b></span>
      <span>覆盖率差 <b class="${diff.coverage_delta_pct >= 0 ? 'hl' : 'danger'}">${diff.coverage_delta_pct >= 0 ? '+' : ''}${fmtPct(diff.coverage_delta_pct)}</b></span>
    </div>
    <dl class="kv">
      <dt>模式</dt><dd>新增 ${esc(p.added)} · 保留 ${esc(p.kept)} · 移除 ${esc(p.removed)}</dd>
      <dt>推荐</dt><dd>新增 ${esc(r.added)} · 保留 ${esc(r.kept)} · 移除 ${esc(r.removed)}</dd>
      <dt>复核</dt><dd>沿用原结论 ${esc(diff.carried_recs)} · 回待复核 ${esc(diff.reset_recs)}</dd>
    </dl>
  </div>`;
}

export const overviewPage = {
  async render(view, ctx) {
    view.innerHTML = '<div class="empty"><div class="mark">采样中…</div></div>';
    let data;
    let patterns;
    try {
      data = await api.get('/api/overview');
      const patternRun = data.active_run || data.latest_run;
      patterns = await api.get('/api/patterns', { run_id: patternRun ? patternRun.id : '', limit: 8 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => overviewPage.render(view, ctx));
      return;
    }
    const wm = data.watermark || {};
    view.innerHTML = `
      ${readouts([
        { label: '会话总量', value: data.sequences_total, unit: '条', hint: `均值 ${data.avg_events} 事件 / 会话` },
        { label: '推荐覆盖率', value: fmtPct(data.active_run ? data.active_run.coverage_pct : 0), hint: data.active_run ? `${data.active_run.covered_users}/${data.active_run.active_users} 位活跃用户` : '尚未切正式版', tone: 'hot' },
        { label: '模式库（正式版）', value: data.patterns_total, unit: '条', hint: data.active_run ? `min_sup ${data.active_run.min_support_pct}% · 最长 ${data.active_run.max_len} 步` : '尚未切正式版' },
        { label: '待复核推荐', value: data.pending_total, unit: '条', hint: data.worker_alive ? 'worker 在岗' : 'worker 心跳超时', tone: 'cool' },
      ])}
      <section class="panel">
        <div class="panel-head"><h2>本批采集水位</h2><span class="meta">watermark · 幂等并入</span></div>
        <div class="panel-body">
          <dl class="kv">
            <dt>时间水位</dt><dd class="mono">${fmtTime(wm.watermark_at)}</dd>
            <dt>已并入事件</dt><dd>${esc(wm.ingested_events || 0)} 条</dd>
            <dt>重复拦截 / 迟到并入</dt><dd>${esc(wm.duplicate_events || 0)} 条 / ${esc(wm.late_events || 0)} 条</dd>
          </dl>
          <div class="legend" style="margin-top:8px">${(wm.sources || []).map((s) => `<span class="mono">${esc(s.source)}：${fmtTime(s.last_event_at)} · 并入 ${esc(s.ingested_events)} · 迟到 ${esc(s.late_events)}</span>`).join('') || '<span>暂无采集源</span>'}</div>
        </div>
      </section>
      <div class="grid-2">
        <section class="panel">
          <div class="panel-head"><h2>信号瀑布</h2><span class="meta">12 buckets · modules</span></div>
          <div class="panel-body">${waterfall(data.waterfall, data.modules)}</div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>模块事件量</h2><span class="meta">7d · events</span></div>
          <div class="panel-body">
            ${data.modules.length ? barList(data.modules) : emptyState({ title: '还没有埋点事件', hint: '管理员先登记事件字典。' })}
            <div class="legend" style="margin-top:12px"><span>转化会话 ${esc(data.converted_total)} / ${esc(data.sequences_total)}（${fmtPct(data.conversion_pct)}）</span></div>
          </div>
        </section>
      </div>
      <div class="grid-2">
        <section class="panel">
          <div class="panel-head"><h2>模式支持度 TOP 8</h2><span class="meta">support / confidence · 正式版</span></div>
          <div class="panel-body">
            ${ladder(patterns.items)}
            <div class="track" style="margin-top:10px">${(patterns.items || [])
              .slice(0, 3)
              .map((item) => `<span class="chip board">${esc(item.seq_key.split('>')[0])} › … <i>${esc(item.length)} 步</i></span>`)
              .join('')}</div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>版本与复核</h2><span class="meta">active / candidate</span></div>
          <div class="panel-body">
            <h3 style="font-size:0.9rem;margin:2px 0">正式版</h3>
            ${runBlock(data.active_run)}
            <h3 style="font-size:0.9rem;margin:12px 0 2px">候选版（最新一批）</h3>
            ${runBlock(data.candidate_run)}
            <h3 style="font-size:0.9rem;margin:12px 0 2px">切换前后差异</h3>
            ${diffBlock(data.switch_diff)}
            <div class="legend" style="margin-top:10px">
              <span>复核：${(data.review || []).map((item) => `${esc(item.label)} ${esc(item.count)}`).join(' · ') || '暂无'}</span>
            </div>
          </div>
        </section>
      </div>`;
  },
};

export default overviewPage;
