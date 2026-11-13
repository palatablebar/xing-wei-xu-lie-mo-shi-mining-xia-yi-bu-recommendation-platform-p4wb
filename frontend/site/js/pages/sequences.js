import { api } from '../api.js';
import { drawer, errorBox, errorMessage, esc, field, fmtTime, kv, table, track } from '../ui.js';

const state = { q: '', converted: '', minLen: '' };

function openSequence(view, ctx, sequence) {
  drawer({
    title: `${sequence.session_id}`,
    subtitle: `${sequence.user_id} · ${sequence.event_count} 个事件 · ${sequence.converted ? '已转化' : '未转化'}`,
    body: `${kv([
      ['会话 ID', `<span class="mono">${esc(sequence.session_id)}</span>`],
      ['用户', `<span class="mono">${esc(sequence.user_id)}</span>`],
      ['事件数', esc(sequence.event_count)],
      ['开始时间', fmtTime(sequence.started_at)],
      ['转化', sequence.converted ? '<span class="badge ok">已转化</span>' : '<span class="badge">未转化</span>'],
      ['模块路径', `<span class="mono">${esc(sequence.modules_path || '—')}</span>`],
    ])}
    <h3 style="font-size:0.95rem;margin:6px 0 2px">事件时序</h3>
    ${track(sequence.events_detail, 60)}
    <h3 style="font-size:0.95rem;margin:6px 0 2px">原始事件码</h3>
    <pre class="code">${esc(JSON.stringify(sequence.events, null, 2))}</pre>`,
  });
}

export const sequencesPage = {
  async render(view, ctx) {
    let data;
    let stats;
    let watermark;
    try {
      [data, stats, watermark] = await Promise.all([
        api.get('/api/sequences', { q: state.q, converted: state.converted, min_len: state.minLen, limit: 60 }),
        api.get('/api/sequences/stats'),
        api.get('/api/sequences/watermark'),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => sequencesPage.render(view, ctx));
      return;
    }
    const wmItems = watermark.items || [];
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head"><h2>采集水位</h2><span class="meta">幂等并入 · 迟到按时间归位</span></div>
        <div class="panel-body">
          ${wmItems.length
            ? table({
                rows: wmItems,
                columns: [
                  { key: 'source', label: '采集源', className: 'mono' },
                  { key: 'last_event_at', label: '时间水位', render: (row) => fmtTime(row.last_event_at) },
                  { key: 'ingested_events', label: '已并入', className: 'num' },
                  { key: 'duplicate_events', label: '重复拦截', className: 'num' },
                  { key: 'late_events', label: '迟到并入', className: 'num' },
                ],
              })
            : '<div class="legend"><span>暂无采集源（演示数据由 seed 初始化）。</span></div>'}
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>序列浏览</h2>
          <span class="meta">${data.total} 条会话 · 均值 ${esc(stats.avg_events)} 事件 · 转化 ${esc(stats.conversion_total)} 条（${esc(stats.conversion_pct)}%）</span>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="sequence-filters">
            ${field({ label: '搜索', name: 'q', value: state.q, placeholder: '会话 ID / 用户 ID' })}
            ${field({ label: '是否转化', name: 'converted', value: state.converted, options: [{ value: '', label: '全部' }, { value: 'true', label: '已转化' }, { value: 'false', label: '未转化' }] })}
            ${field({ label: '最少事件数', name: 'min_len', type: 'number', value: state.minLen, placeholder: '8' })}
            <div class="spacer"></div>
          </div>
        </div>
        ${table({
          rows: data.items,
          empty: { title: '没有匹配会话', hint: '放宽筛选条件，或先跑一次 seed 导入埋点数据。' },
          columns: [
            { key: 'session_id', label: '会话', className: 'mono' },
            { key: 'user_id', label: '用户', className: 'mono' },
            { key: 'events_detail', label: '事件轨道', render: (row) => track(row.events_detail, 7) },
            { key: 'event_count', label: '事件数', className: 'num' },
            { key: 'converted', label: '转化', render: (row) => (row.converted ? '<span class="badge ok">已转化</span>' : '<span class="badge">—</span>') },
            { key: 'started_at', label: '开始时间', render: (row) => fmtTime(row.started_at) },
          ],
        })}
      </section>`;
    const filters = view.querySelector('#sequence-filters');
    filters?.addEventListener('change', () => {
      state.q = filters.querySelector('[name=q]').value.trim();
      state.converted = filters.querySelector('[name=converted]').value;
      state.minLen = filters.querySelector('[name=min_len]').value;
      sequencesPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        sequencesPage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const item = data.items.find((entry) => String(entry.id) === row.dataset.id);
        if (item) openSequence(view, ctx, item);
      });
    });
  },
};

export default sequencesPage;
