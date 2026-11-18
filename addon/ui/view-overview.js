/* ==========================================================================
   WattMosaic Engine panel — view-overview.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { slots } from './slots.js';
import { SOURCE_SWATCH, ago, badge, card, duration, el, emptyState, fetchOverview, kv, label, notice, num, percent, render, setConnection, setFooter, sourceTone, statCard, state, statusBadge, tierBadge, titleCase, toneFor, watts } from './core.js';

async function renderOverview(root, signal) {
  const { health, status, snapshot, devices, residuals, mapping } = await fetchOverview(signal);
  if (signal.aborted) return;

  state.lastHealth = health;
  state.lastSnapshot = snapshot;
  state.lastDevices = devices;
  setConnection(health);
  setFooter(health);
  root.append(overviewHeader());

  const recon = (snapshot && snapshot.reconciliation) || {};
  const topology = (health.details && health.details.topology) || {};

  if (topology.configured === false) {
    root.append(
      notice(
        'No topology configured',
        topology.reason ||
          'The engine is ingesting samples but has no CT topology, so it cannot emit device values. Configure the connection in the WattMosaic integration.',
        'warn'
      )
    );
  }
  if (health.status === 'error') {
    root.append(
      notice(
        'Engine reports an error',
        health.database && health.database !== 'ok'
          ? `Database: ${health.database}`
          : 'See Diagnostics for the full report.',
        'bad'
      )
    );
  }

  root.append(overviewStats(health, status, recon));
  root.append(overviewHero(recon, health, snapshot));

  const cols = el('div', { class: 'grid cols-2' }, [
    reconciliationCard(recon),
    systemCard(health, status),
  ]);
  root.append(cols);

  root.append(overviewDevices(devices, snapshot));

  const groupRanges = slots.groupRangesCard(devices);
  if (groupRanges) root.append(groupRanges);
  if (residuals) root.append(slots.residualDecompositionCard(residuals));
  if (mapping) root.append(slots.mappingMatrixCard(mapping));
}

function overviewHeader() {
  return el('div', { class: 'page-head' }, [
    el('div', {}, [
      el('h1', {}, 'Home overview'),
      el('p', { class: 'lede' },
        'Measured total, what the engine could attribute, and the residual it refuses to hide.'),
    ]),
    el('div', { class: 'head-actions' }, [
      el('button', { class: 'btn', type: 'button', onclick: () => render() }, 'Refresh'),
    ]),
  ]);
}

function overviewStats(health, status, recon) {
  const accounted = recon.accounted_fraction;
  const qualityScore =
    (status && status.quality && status.quality.overall_score) ??
    (state.lastSnapshot && state.lastSnapshot.quality && state.lastSnapshot.quality.overall_score);
  return el('div', { class: 'grid cols-5', style: 'margin-bottom:16px' }, [
    statCard('Main power', num(recon.main_power_w, 0), {
      unit: 'W',
      hint: 'Sum of confirmed main CTs',
      tone: 'accent',
    }),
    statCard('Accounted', accounted === undefined ? '—' : percent(accounted, 0), {
      hint: 'Attributed to a device',
      tone: accounted >= 0.8 ? 'ok' : accounted >= 0.4 ? 'warn' : null,
    }),
    statCard('Unknown residual', num(recon.unknown_residual_w, 0), {
      unit: 'W',
      hint: 'Never redistributed',
      tone: 'warn',
    }),
    statCard('Engine', label(health.status), {
      hint: health.engine_version ? `v${health.engine_version}` : '',
      tone: toneFor(health.status),
    }),
    statCard('Data quality', qualityScore === undefined ? '—' : percent(qualityScore, 0), {
      hint: 'Aggregate channel health',
      tone: qualityScore >= 0.85 ? 'ok' : qualityScore >= 0.6 ? 'warn' : 'bad',
    }),
  ]);
}

function overviewHero(recon, health, snapshot) {
  const total = el('div', { class: 'hero-total' }, [
    el('span', { class: 'watts' }, num(recon.main_power_w, 0)),
    el('span', { class: 'unit' }, 'W'),
  ]);

  // Phase values are optional: a single-phase install reports neither, and
  // two "—" pills would imply a measurement that does not exist.
  const hasPhases =
    (recon.l1_power_w !== null && recon.l1_power_w !== undefined) ||
    (recon.l2_power_w !== null && recon.l2_power_w !== undefined);
  const phases = hasPhases
    ? el('div', { class: 'hero-phases' }, [
        phasePill('L1', recon.l1_power_w),
        phasePill('L2', recon.l2_power_w),
      ])
    : null;

  const meta = el('div', { class: 'hero-meta' }, [
    el('span', {}, `Topology v${recon.topology_version ?? '—'} · ${label(health.details?.topology?.status)}`),
    el('span', {}, `Updated ${ago((snapshot && snapshot.timestamp) || recon.timestamp)}`),
    el('span', {}, `Quality ${label(recon.quality_status)}`),
  ]);

  const main = el('section', { class: 'card hero-main' }, [
    el('div', { class: 'stat-label' }, 'Total incoming power'),
    total,
    phases,
    meta,
  ]);

  const breakdown = reconciliationBreakdown(recon);
  const side = card('Where the watts went', [breakdown], {
    foot: 'The residual stays unknown. It is a measurement of what the engine cannot attribute, not a device.',
  });

  return el('div', { class: 'hero' }, [main, side]);
}

function phasePill(name, value) {
  return el('div', { class: 'phase-pill' }, [
    el('div', { class: 'lbl' }, name),
    el('div', { class: 'v' }, value === null || value === undefined ? '—' : watts(value)),
  ]);
}

function reconciliationBreakdown(recon) {
  const rows = [
    ['direct_measured', 'Direct measured', recon.direct_measured_w, 'var(--ok)'],
    ['inferred', 'Inferred', recon.inferred_w, 'var(--info)'],
    ['model_estimated', 'Model estimated', recon.model_estimated_w, 'var(--accent)'],
    ['configured_prior', 'Configured prior', recon.configured_prior_w, 'var(--warn)'],
    ['unknown', 'Unknown residual', recon.unknown_residual_w, 'var(--mute)'],
  ].filter(([, , value]) => value !== undefined);

  const total = Math.max(
    Number(recon.main_power_w) || 0,
    rows.reduce((acc, [, , value]) => acc + (Number(value) || 0), 0)
  );

  const meter = el(
    'div',
    { class: 'meter', role: 'img', 'aria-label': 'Power attribution breakdown' },
    rows.map(([key, , value]) => {
      const pct = total > 0 ? (Math.max(0, Number(value) || 0) / total) * 100 : 0;
      return el('span', {
        class:
          key === 'direct_measured'
            ? 'seg-direct'
            : key === 'inferred'
              ? 'seg-inferred'
              : key === 'model_estimated'
                ? 'seg-model'
                : key === 'configured_prior'
                  ? 'seg-prior'
                  : 'seg-unknown',
        style: `width:${pct.toFixed(2)}%`,
        title: `${label(key)} ${watts(value)}`,
      });
    })
  );

  const legend = el(
    'div',
    { class: 'legend' },
    rows.map(([, name, value, colour]) =>
      el('span', { class: 'legend-item' }, [
        el('span', { class: 'legend-swatch', style: `background:${colour}` }),
        el('span', {}, name),
        el('span', { class: 'val' }, watts(value)),
      ])
    )
  );

  return el('div', {}, [meter, legend]);
}

function reconciliationCard(recon) {
  const main = Number(recon.main_power_w) || 0;
  const residual = Number(recon.unknown_residual_w) || 0;
  // The residual share is the unknown part of the measured mains.  It is not
  // ``reconciliation_error_ratio``: that is the least-squares fit error of the
  // device estimates against the CT rows, a different diagnostic.
  const residualShare = main > 0 ? residual / main : undefined;
  return card(
    'Reconciliation',
    [
      kv([
        ['Main (L1 + L2)', watts(recon.main_power_w)],
        ['Direct measured', watts(recon.direct_measured_w)],
        ['Inferred', watts(recon.inferred_w)],
        ['Model estimated', watts(recon.model_estimated_w)],
        ['Configured prior', watts(recon.configured_prior_w)],
        ['Unknown residual', watts(recon.unknown_residual_w)],
        ['Residual share', residualShare === undefined ? '—' : percent(residualShare, 1)],
        ['Measurement fit error', watts(recon.reconciliation_error_w)],
      ]),
      (recon.explanation || []).length
        ? el(
            'ul',
            { class: 'explain', style: 'margin-top:14px' },
            (recon.explanation || []).map((item) =>
              el('li', {}, [
                el('span', { class: 'marker', 'aria-hidden': 'true' }, '·'),
                el('span', {}, titleCase(item)),
              ])
            )
          )
        : null,
    ],
    { foot: 'Residual = measured mains minus what the model could attribute.' }
  );
}

function systemCard(health, status) {
  const inference = (status && status.inference) || {};
  const learning = (status && status.learning) || {};
  const details = health.details || {};
  const rows = [
    ['Home Assistant', health.ha_connected ? badge('Connected', 'ok', { dot: true }) : badge('Disconnected', 'warn', { dot: true })],
    ['Database', statusBadge(health.database)],
    ['Queue depth', String(health.queue_depth ?? '—')],
    ['Inference backend', details.inference_backend || '—'],
    ['Inference cycles', inference.cycles === undefined ? '—' : String(inference.cycles)],
    ['Last inference', ago(health.inference_last_run_at)],
    ['Learning', learning.active ? badge('Active', 'ok', { dot: true }) : badge('Idle', 'mute', { dot: true })],
    ['Uptime', duration(health.uptime_seconds)],
    ['Contract', health.contract_version || '—'],
  ];
  return card('Engine', [kv(rows)], {
    foot: 'The engine reads Home Assistant; it never switches a device.',
  });
}

function overviewDevices(devices, snapshot) {
  const list = Array.isArray(devices) ? devices : [];
  const withPower = list
    .map((d) => ({ d, result: d.latest_result || null }))
    .filter(({ result }) => result && Number(result.power_w) > 0)
    .sort((a, b) => Number(b.result.power_w) - Number(a.result.power_w))
    .slice(0, 8);

  const body = withPower.length
    ? withPower.map(({ d, result }) => deviceMiniRow(d, result))
    : emptyState(
        'No device values yet',
        list.length
          ? 'Devices exist but none has a power value yet. Direct CTs appear first; inferred and learned values follow as the engine observes the home.'
          : 'No topology devices. Configure the WattMosaic integration to map your CTs and devices.'
      );

  return el('section', { class: 'card', style: 'margin-top:16px' }, [
    el('div', { class: 'card-head' }, [
      el('h2', {}, 'Top consumers'),
      el('span', { class: 'spacer' }),
      el('a', { class: 'btn btn-sm btn-ghost', href: '#/devices' }, `All devices (${list.length})`),
    ]),
    el('div', {}, body),
  ]);
}

function deviceMiniRow(device, result) {
  const p = Number(result.power_w) || 0;
  const max = 5000;
  const width = Math.min(100, (p / max) * 100);
  return el('a', { class: 'device-mini', href: `#/device/${encodeURIComponent(device.device_id)}` }, [
    el('div', { class: 'who' }, [
      el('div', { class: 'nm' }, device.name || device.device_id),
      el('div', { class: 'sub' }, [
        badge(label(result.source), sourceTone(result.source)),
        tierBadge(result.confidence_tier || device.confidence_tier),
      ]),
    ]),
    el('div', { class: 'bar' }, [
      el('div', { class: 'meter', role: 'img', 'aria-label': `${label(result.source)} ${watts(p)}` }, [
        el('span', {
          style: `width:${width}%;background:${SOURCE_SWATCH[result.source] || 'var(--mute)'}`,
        }),
      ]),
    ]),
    el('div', { class: 'watts' }, [watts(p), el('span', { class: 'unit' }, '')]),
  ]);
}

export {
  renderOverview,
  overviewHeader,
  overviewStats,
  overviewHero,
  phasePill,
  reconciliationBreakdown,
  reconciliationCard,
  systemCard,
  overviewDevices,
  deviceMiniRow
};
