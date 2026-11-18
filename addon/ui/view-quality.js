/* ==========================================================================
   WattMosaic Engine panel — view-quality.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { ago, badge, card, el, emptyState, get, kWh, label, percent, render, state, statusBadge, watts } from './core.js';

/* --- Quality ---------------------------------------------------------- */

async function renderQuality(root, signal) {
  const report = await get('/quality', signal);
  if (signal.aborted) return;

  root.append(
    el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('h1', {}, 'Data quality'),
        el('p', { class: 'lede' },
          'Per-channel health and the concrete problems the engine found. A low score is information, not a failure.'),
      ]),
      el('div', { class: 'head-actions' }, [
        el('button', { class: 'btn', type: 'button', onclick: () => render() }, 'Re-evaluate view'),
      ]),
    ])
  );

  const score = Number(report.overall_score);
  const tone = score >= 0.85 ? 'ok' : score >= 0.6 ? 'warn' : 'bad';
  const channels = Object.entries(report.channels || {});
  const issues = report.issues || [];

  root.append(
    el('section', { class: 'card', style: 'margin-bottom:16px' }, [
      el('div', { class: 'card-body' }, [
        el('div', { class: 'score-dial' }, [
          el('div', {
            class: 'dial',
            style: `--v:${Number.isFinite(score) ? Math.round(score * 100) : 0};--tone:var(--${tone})`,
            role: 'img',
            'aria-label': `Overall data quality ${percent(score, 0)}`,
          }, el('span', { class: 'n' }, percent(score, 0))),
          el('div', {}, [
            el('h2', {}, 'Overall channel health'),
            el('p', { class: 'stat-hint' },
              `${channels.length} channels evaluated · ${issues.length} issue${issues.length === 1 ? '' : 's'} · ${ago(report.timestamp)}`),
            el('div', { style: 'display:flex;gap:8px;margin-top:10px;flex-wrap:wrap' }, [
              statusBadge(score >= 0.85 ? 'good' : score >= 0.6 ? 'degraded' : 'bad'),
              issues.length ? badge(`${issues.length} open issues`, 'warn') : badge('No issues', 'ok'),
            ]),
          ]),
        ]),
      ]),
    ])
  );

  root.append(
    el('div', { class: 'grid cols-2' }, [
      qualityIssuesCard(issues),
      qualityChannelsCard(channels),
    ])
  );
}

function qualityIssuesCard(issues) {
  return card(
    'Issues',
    [
      issues.length
        ? el('div', {}, issues.map((text) =>
            el('div', { class: 'issue' }, [
              el('span', { class: 'sev', 'aria-hidden': 'true' }),
              el('div', { class: 'txt' }, String(text)),
            ])
          ))
        : el('p', { class: 'stat-hint' }, 'The engine reported no data-quality issues.'),
    ],
    { flush: true, foot: 'Issues are deduplicated by the engine, one per cluster.' }
  );
}

function qualityChannelsCard(channels) {
  const filter = state.qualityFilter;
  const visible = channels.filter(([, ch]) => {
    if (filter === 'all') return true;
    const status = String(ch.status || '').toLowerCase();
    if (filter === 'issues') return (ch.issues || []).length > 0;
    return status === filter;
  });

  const filterBar = el('div', { class: 'segmented', role: 'group', 'aria-label': 'Filter channels' },
    [['all', 'All'], ['good', 'Good'], ['degraded', 'Degraded'], ['issues', 'With issues']].map(([key, text]) =>
      el('button', {
        type: 'button',
        'aria-pressed': String(filter === key),
        onclick: () => { state.qualityFilter = key; render(); },
      }, text)
    ));

  return el('section', { class: 'card' }, [
    el('div', { class: 'card-head' }, [
      el('h2', {}, 'Channels'),
      el('span', { class: 'spacer' }),
      filterBar,
    ]),
    el('div', { class: 'card-body flush' },
      visible.length
        ? visible.map(([name, ch]) => qualityChannelRow(name, ch))
        : emptyState('No channels match', 'Change the filter to see the evaluated channels.')),
  ]);
}

function qualityChannelRow(name, ch) {
  const issues = ch.issues || [];
  const value =
    ch.value_kwh !== null && ch.value_kwh !== undefined
      ? kWh(ch.value_kwh)
      : ch.value_w !== null && ch.value_w !== undefined
        ? watts(ch.value_w)
        : '—';
  return el('div', { class: 'qchannel' }, [
    el('div', { class: 'qc-head' }, [
      el('span', { class: 'qc-name' }, name),
      statusBadge(ch.status),
      ch.domain ? badge(String(ch.domain), 'mute') : null,
      el('span', { class: 'spacer' }),
      el('span', { class: 'stat-hint' }, value),
      el('span', { style: 'min-width:52px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600' },
        percent(ch.score, 0)),
    ]),
    issues.length
      ? el('div', { class: 'qc-detail' }, issues.join(' · '))
      : null,
    ch.repeated ? el('div', { class: 'qc-detail' }, 'Repeated value: not counted as a new measurement.') : null,
  ]);
}

export {
  renderQuality,
  qualityIssuesCard,
  qualityChannelsCard,
  qualityChannelRow
};
