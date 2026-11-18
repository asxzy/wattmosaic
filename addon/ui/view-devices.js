/* ==========================================================================
   WattMosaic Engine panel — view-devices.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { TIER_ORDER, ago, badge, card, clear, clock, describeError, duration, el, emptyState, fetchDevice, get, kWh, kv, label, notify, num, percent, sourceTone, statCard, state, statusBadge, tierBadge, tierInfo, titleCase, toneFor, watts } from './core.js';

/* --- Devices ---------------------------------------------------------- */

async function renderDevices(root, signal) {
  const devices = await get('/devices', signal);
  if (signal.aborted) return;
  state.lastDevices = devices;

  root.append(
    el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('h1', {}, 'Devices'),
        el('p', { class: 'lede' },
          'Every load in the active topology with its latest estimate, evidence grade and measurement source.'),
      ]),
    ])
  );

  if (!Array.isArray(devices) || devices.length === 0) {
    root.append(
      emptyState(
        'No devices in the topology',
        'The engine has no devices to disaggregate. Map CTs and devices in the WattMosaic integration; the active topology is used automatically as soon as devices appear.',
        { action: el('a', { class: 'btn btn-primary', href: '#/topology' }, 'View topology') }
      )
    );
    return;
  }

  const summary = deviceSummary(devices);
  root.append(summary);
  root.append(deviceToolbar(devices));

  const container = el('div', { class: 'device-grid', id: 'device-grid' });
  root.append(container);
  paintDeviceGrid(container, devices);
}

function deviceSummary(devices) {
  const totals = { measured: 0, inferred: 0, modelled: 0, prior: 0, unknown: 0 };
  for (const d of devices) {
    const r = d.latest_result;
    if (!r) { totals.unknown += 1; continue; }
    switch (r.source) {
      case 'direct_measured': totals.measured += 1; break;
      case 'inferred': totals.inferred += 1; break;
      case 'model_estimated': totals.modelled += 1; break;
      case 'configured_prior': totals.prior += 1; break;
      default: totals.unknown += 1;
    }
  }
  return el('div', { class: 'grid cols-5', style: 'margin-bottom:16px' }, [
    statCard('Devices', String(devices.length), { hint: 'In the active topology' }),
    statCard('Direct measured', String(totals.measured), {
      hint: 'Own CT',
      tone: totals.measured ? 'ok' : null,
    }),
    statCard('Inferred', String(totals.inferred), {
      hint: 'Solved from shared CTs',
      tone: totals.inferred ? 'accent' : null,
    }),
    statCard('Model / prior', String(totals.modelled + totals.prior), {
      hint: 'No direct evidence',
      tone: totals.modelled + totals.prior ? 'warn' : null,
    }),
    statCard('Unknown', String(totals.unknown), {
      hint: 'No honest value yet',
      tone: totals.unknown ? 'bad' : null,
    }),
  ]);
}

function deviceToolbar(devices) {
  const search = el('input', {
    type: 'search',
    placeholder: 'Filter by name or id…',
    value: state.devicesFilter,
    'aria-label': 'Filter devices',
    oninput: (event) => {
      state.devicesFilter = event.target.value;
      paintDeviceGrid(document.getElementById('device-grid'), devices);
    },
  });

  const sort = el(
    'div',
    { class: 'segmented', role: 'group', 'aria-label': 'Sort devices' },
    [
      ['power', 'Power'],
      ['name', 'Name'],
      ['tier', 'Evidence'],
    ].map(([key, text]) =>
      el('button', {
        type: 'button',
        'aria-pressed': String(state.devicesSort === key),
        onclick: (event) => {
          state.devicesSort = key;
          for (const btn of event.currentTarget.parentNode.children) {
            btn.setAttribute('aria-pressed', String(btn.textContent === text));
          }
          paintDeviceGrid(document.getElementById('device-grid'), devices);
        },
      }, text)
    )
  );

  return el('div', { class: 'toolbar' }, [
    el('label', { class: 'field' }, ['Search', search]),
    el('span', { class: 'spacer' }),
    el('span', { class: 'field', style: 'flex-direction:row;align-items:center;gap:10px' }, [
      el('span', {}, 'Sort'),
      sort,
    ]),
  ]);
}

function paintDeviceGrid(container, devices) {
  if (!container) return;
  clear(container);

  let list = [...devices];
  const query = state.devicesFilter.trim().toLowerCase();
  if (query) {
    list = list.filter((d) =>
      `${d.name || ''} ${d.device_id || ''}`.toLowerCase().includes(query)
    );
  }
  const tierRank = (d) => tierInfo(d.latest_result && d.latest_result.confidence_tier).index;
  const powerOf = (d) => Number((d.latest_result && d.latest_result.power_w) || 0);
  if (state.devicesSort === 'name') {
    list.sort((a, b) => String(a.name || a.device_id).localeCompare(String(b.name || b.device_id)));
  } else if (state.devicesSort === 'tier') {
    list.sort((a, b) => tierRank(b) - tierRank(a) || powerOf(b) - powerOf(a));
  } else {
    list.sort((a, b) => powerOf(b) - powerOf(a));
  }

  if (list.length === 0) {
    container.append(
      emptyState('No matching devices', 'Adjust the filter to see devices again.')
    );
    return;
  }
  for (const device of list) container.append(deviceCard(device));
}

function deviceCard(device) {
  const result = device.latest_result || null;
  // ``power_w`` is a required float in the contract, but a stored row from an
  // older release can carry null.  ``Number(null)`` is 0, which would claim a
  // measured 0 W; only a real finite number is a value.
  const hasPower = result !== null && result.power_w !== null && result.power_w !== undefined
    && Number.isFinite(Number(result.power_w));
  const source = result ? result.source : null;
  const tier = (result && result.confidence_tier) || device.confidence_tier;
  const model = device.model;

  const powerNode = hasPower
    ? el('div', { class: 'dc-power' }, [
        el('span', { class: 'n' }, num(result.power_w, Math.abs(Number(result.power_w)) >= 100 ? 0 : 1)),
        el('span', { class: 'u' }, 'W'),
      ])
    : el('div', { class: 'dc-power unknown' }, [
        el('span', { class: 'n' }, 'Unknown'),
      ]);

  const tags = el('div', { class: 'dc-tags' }, [
    result ? badge(label(source), sourceTone(source)) : badge('No value', 'mute'),
    result ? tierBadge(tier) : null,
    result && result.quality_status && result.quality_status !== 'good'
      ? statusBadge(result.quality_status)
      : null,
  ]);

  return el(
    'a',
    {
      class: 'device-card',
      href: `#/device/${encodeURIComponent(device.device_id)}`,
      'aria-label': `${device.name || device.device_id}: ${hasPower ? watts(result.power_w) : 'unknown'}`,
    },
    [
      el('div', { class: 'dc-head' }, [
        el('div', { style: 'min-width:0;flex:1 1 auto' }, [
          el('div', { class: 'dc-name' }, device.name || device.device_id),
          el('div', { class: 'dc-id' }, device.device_id),
        ]),
        device.double_pole ? badge('240 V', 'info') : null,
      ]),
      el('div', { class: 'dc-body' }, [
        powerNode,
        tags,
        result && result.operating_stage
          ? el('div', { class: 'stat-hint' }, `Stage: ${titleCase(result.operating_stage)}`)
          : null,
      ]),
      el('div', { class: 'dc-foot' }, [
        el('span', {}, model ? `Model v${model.version} · ${label(model.status)}` : 'No learned model'),
        el('span', {}, result ? ago(result.timestamp) : '—'),
      ]),
    ]
  );
}

/* --- Device detail ---------------------------------------------------- */

async function renderDevice(root, signal) {
  const deviceId = state.deviceId;
  const { device, models } = await fetchDevice(deviceId, signal);
  if (signal.aborted) return;

  const result = device.latest_result || null;
  const topology = device.topology || null;

  root.append(
    el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('div', { style: 'display:flex;gap:10px;align-items:center;margin-bottom:4px' }, [
          el('a', { class: 'btn btn-sm btn-ghost', href: '#/devices' }, '← Devices'),
        ]),
        el('h1', {}, device.name || device.device_id || 'Device'),
        el('p', { class: 'lede' }, device.device_id || ''),
      ]),
      el('div', { class: 'head-actions' }, [
        el('button', {
          class: 'btn btn-sm',
          type: 'button',
          onclick: () => copyExport(device.device_id),
        }, 'Copy export'),
      ]),
    ])
  );

  if (!result && !topology) {
    root.append(
      emptyState('Device not found', 'The engine does not know this device. It may have been removed from the topology.')
    );
    return;
  }

  const mainCol = el('div', { style: 'display:flex;flex-direction:column;gap:16px' });
  const sideCol = el('div', { class: 'detail-side' });

  mainCol.append(deviceHero(device, result));
  mainCol.append(deviceHistoryCard(device));
  mainCol.append(deviceEvidenceCard(result));
  // Additive B4-UIb: this device's model versions come from the /models payload
  // this view already fetches, so no extra request is needed.
  mainCol.append(versionHistoryCard(topology, models, null));
  if (device.configuration) mainCol.append(deviceConfigurationCard(device.configuration));

  sideCol.append(deviceSourceCard(result));
  sideCol.append(deviceTierCard(result));
  sideCol.append(deviceModelCard(device, models));
  if (topology) sideCol.append(deviceTopologyCard(topology));

  root.append(el('div', { class: 'detail-layout' }, [mainCol, sideCol]));
}

function deviceHero(device, result) {
  const hasPower = result !== null && result.power_w !== null && result.power_w !== undefined
    && Number.isFinite(Number(result.power_w));
  return el('section', { class: 'card' }, [
    el('div', { class: 'card-body' }, [
      el('div', { class: 'detail-hero' }, [
        el('div', {}, [
          el('div', { class: 'stat-label' }, 'Estimated power'),
          el('div', { class: 'big' }, [
            hasPower ? num(result.power_w, Math.abs(Number(result.power_w)) >= 100 ? 0 : 1) : '—',
            el('span', { class: 'unit' }, hasPower ? 'W' : ''),
          ]),
          !hasPower
            ? el('p', { class: 'stat-hint', style: 'margin-top:6px;max-width:40ch' },
                'No honest device-level value. The load stays in the unknown residual.')
            : null,
        ]),
        el('div', { class: 'meta' }, [
          result ? badge(label(result.source), sourceTone(result.source), { dot: true }) : null,
          result ? tierBadge(result.confidence_tier) : null,
          result && result.energy_kwh !== null && result.energy_kwh !== undefined
            ? el('span', { class: 'chip' }, kWh(result.energy_kwh))
            : null,
          result && result.operating_stage
            ? el('span', { class: 'chip' }, `Stage: ${titleCase(result.operating_stage)}`)
            : null,
        ]),
      ]),
    ]),
    el('div', { class: 'card-foot' }, [
      el('span', {}, `Last update ${ago(result && result.timestamp)}`),
      result && result.measurement_age_seconds !== undefined
        ? el('span', {}, ` · Measurement age ${duration(result.measurement_age_seconds)}`)
        : null,
    ]),
  ]);
}

function deviceHistoryCard(device) {
  const history = (device.history || []).slice().reverse();
  if (!history.length) {
    return card('History', [
      emptyState('No stored history', 'Inference results appear here once the engine has run a few cycles for this device.'),
    ]);
  }
  return card(
    'Recent history',
    [
      sparkline(history),
      el('div', { class: 'table-wrap', style: 'margin-top:14px' }, [
        el('table', { class: 'data' }, [
          el('thead', {}, el('tr', {}, [
            el('th', {}, 'Time'),
            el('th', { class: 'num' }, 'Power'),
            el('th', {}, 'Source'),
            el('th', { class: 'num' }, 'Confidence'),
            el('th', {}, 'Quality'),
            el('th', {}, 'Stage'),
          ])),
          el('tbody', {},
            history.slice(0, 12).map((row) =>
              el('tr', {}, [
                el('td', {}, ago(row.timestamp)),
                el('td', { class: 'num' }, watts(row.power_w)),
                el('td', {}, badge(label(row.source), sourceTone(row.source))),
                el('td', { class: 'num' }, row.confidence === null || row.confidence === undefined ? '—' : percent(row.confidence, 0)),
                el('td', {}, statusBadge(row.quality_status)),
                el('td', {}, row.operating_stage ? titleCase(row.operating_stage) : '—'),
              ])
            )),
        ]),
      ]),
    ],
    { foot: 'Newest first. Historical rows are stored inference results, not re-simulated values.' }
  );
}

function sparkline(history) {
  // ``history`` arrives newest-first from the API, so sort by time before
  // mapping to x: computing the domain from the reversed array produced
  // enormous negative coordinates and painted the polygon across the page.
  const points = history
    .map((row) => ({ t: new Date(row.timestamp).getTime(), v: Number(row.power_w) }))
    .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
    .sort((a, b) => a.t - b.t);
  if (points.length < 2) return el('div', { class: 'stat-hint' }, 'Not enough points for a chart.');

  const w = 600;
  const h = 56;
  const pad = 4;
  const minT = points[0].t;
  const maxT = points[points.length - 1].t;
  const spanT = maxT - minT || 1;
  const maxV = Math.max(1, ...points.map((p) => p.v));
  const minV = Math.min(0, ...points.map((p) => p.v));
  const spanV = maxV - minV || 1;
  const x = (t) => pad + ((t - minT) / spanT) * (w - pad * 2);
  const y = (v) => h - pad - ((v - minV) / spanV) * (h - pad * 2);
  const line = points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
  const area = `${pad},${h - pad} ${line} ${w - pad},${h - pad}`;

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'spark');
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `Power over the last ${points.length} results, peak ${watts(maxV)}`);
  const areaNode = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  areaNode.setAttribute('class', 'area');
  areaNode.setAttribute('points', area);
  const lineNode = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  lineNode.setAttribute('points', line);
  svg.append(areaNode, lineNode);
  return svg;
}

function deviceEvidenceCard(result) {
  const explanation = (result && result.explanation) || [];
  const tierReasons = (result && result.tier_reasons) || [];
  const supporting = (result && result.supporting_ct_channels) || [];
  const items = [
    ...explanation.map((text) => ({ kind: 'explanation', text })),
    ...tierReasons.map((text) => ({ kind: 'tier', text })),
  ];

  return card(
    'Why this number',
    [
      items.length
        ? el(
            'ul',
            { class: 'explain' },
            items.map((item) =>
              el('li', {}, [
                el('span', { class: `marker${item.kind === 'tier' ? ' marker-tier' : ''}`, 'aria-hidden': 'true' },
                  item.kind === 'tier' ? 'T' : '·'),
                el('span', {}, titleCase(item.text)),
              ])
            )
          )
        : el('p', { class: 'stat-hint' }, 'The engine recorded no explanation for this result.'),
      supporting.length
        ? el('div', { style: 'margin-top:14px' }, [
            el('div', { class: 'stat-label', style: 'margin-bottom:8px' }, 'Supporting CT channels'),
            el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' },
              supporting.map((ct) => el('span', { class: 'chip chip-ct' }, ct))),
          ])
        : null,
    ],
    { foot: 'Explanations are generated by the engine, not by this panel.' }
  );
}

function deviceSourceCard(result) {
  const rows = [
    ['Source', result ? badge(label(result.source), sourceTone(result.source)) : '—'],
    ['Confidence', result ? percent(result.confidence, 0) : '—'],
    ['Calibrated', result ? (result.confidence_is_calibrated ? 'Yes' : 'No — engineering score') : '—'],
    ['Quality', result ? statusBadge(result.quality_status) : '—'],
    ['Measurement age', result ? duration(result.measurement_age_seconds) : '—'],
    ['Sample count', result ? String(result.sample_count ?? '—') : '—'],
    ['Model', result && result.model_id ? `${result.model_id} v${result.model_version ?? '—'}` : '—'],
  ];
  return card('Estimate', [kv(rows)], {
    foot: 'Confidence is an engineering score, not a probability.',
  });
}

function deviceTierCard(result) {
  const current = tierInfo(result && result.confidence_tier);
  const list = (state.tiers && state.tiers.tiers) || [];
  const rows = (list.length ? list : TIER_ORDER.map((tier) => ({ tier, label: tierInfo(tier).label }))).map(
    (entry) => {
      const info = tierInfo(entry.tier);
      return el('div', { class: `ts-row${entry.tier === current.key ? ' active' : ''}` }, [
        el('span', { class: 'nm' }, entry.label || info.label),
        el('span', { class: 'ds' }, entry.description ? shorten(entry.description, 96) : ''),
      ]);
    }
  );
  return card(
    'Evidence grade',
    [
      el('div', { style: 'margin-bottom:12px;display:flex;align-items:center;gap:8px' }, [
        tierBadge(current.key),
        el('span', { class: 'stat-hint' }, 'An evidence grade, not a probability.'),
      ]),
      el('div', { class: 'tier-scale' }, rows),
    ],
    { foot: 'Tiers are ordered from "nothing supports it" to "measured and corroborated".' }
  );
}

function deviceModelCard(device, models) {
  const active = (models && models.active) || device.model;
  if (!active) {
    return card('Model', [
      emptyState(
        'No learned model',
        'The engine learns a model only after repeated, validated cycles for this device.',
        { tone: 'warn' }
      ),
    ]);
  }
  return card('Model', [
    kv([
      ['Type', titleCase(active.model_type || '—')],
      ['Version', String(active.version ?? '—')],
      ['Status', statusBadge(active.status)],
      ['Samples', String(active.sample_count ?? '—')],
      ['Confidence', active.confidence === null || active.confidence === undefined ? '—' : percent(active.confidence, 0)],
      ['Trained to', active.trained_to ? ago(active.trained_to) : '—'],
    ]),
  ]);
}

function deviceTopologyCard(topology) {
  const rows = [
    ['Phase', label(topology.phase)],
    ['Double pole', topology.double_pole ? 'Yes' : 'No'],
    ['Status', statusBadge(topology.topology_status)],
    ['Monitored by', (topology.branch_ct_ids || []).join(', ') || '—'],
    ['Branch CTs', (topology.branch_ct_ids || []).join(', ') || '—'],
    ['Parent device', topology.parent_device_id || '—'],
  ];
  return card('Placement', [kv(rows)]);
}

function deviceConfigurationCard(configuration) {
  const rows = [
    ['Device type', titleCase(configuration.device_type || '—')],
    ['Connectivity', titleCase(configuration.connectivity || '—')],
    ['Behavior', titleCase(configuration.behavior || '—')],
    ['Expected on', configuration.expected_on_w === null || configuration.expected_on_w === undefined ? '—' : watts(configuration.expected_on_w)],
    ['Expected standby', configuration.expected_standby_w === null || configuration.expected_standby_w === undefined ? '—' : watts(configuration.expected_standby_w)],
    ['Load entities', (configuration.load_entity_ids || []).join(', ') || '—'],
    ['Mode entities', (configuration.mode_entity_ids || []).join(', ') || '—'],
    ['Monitored by', (configuration.monitored_by_ct_ids || []).join(', ') || '—'],
  ];
  return card('Declared configuration', [kv(rows)], {
    foot: 'Declarations are user knowledge, never inferred from a name or an area.',
  });
}

const shorten = (text, max) => {
  const value = String(text || '');
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
};

async function copyExport(deviceId) {
  try {
    const payload = await get(`/export/${encodeURIComponent(deviceId)}`);
    await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
    notify('Export copied', `${deviceId} JSON is on the clipboard.`);
  } catch (err) {
    notify('Export failed', describeError(err), 'bad');
  }
}


/* --- Derived backend data (completion sprint B4-UIa) ------------------- *
 * Three additive renderers for data the engine now publishes: the residual
 * decomposition, the derived device/reader mapping matrix, and identifiability
 * group ranges.  Every field comes from an API payload and is inserted as text.
 * A value the engine did not publish (``null``/absent) reads "—", never 0 W,
 * and a non-identifiable device shows a *group range*, never a fabricated
 * per-device watt.
 * ---------------------------------------------------------------------- */

// GET /api/v1/residuals -> { window_hours, count, components: [...] }.
// A component carries the signed residual, its three-way split and the
// estimator/resolved state.  Older payloads may omit noise_w or the estimator;
// ``watts``/the fallback render those as "—" rather than inventing a number.
function residualDecompositionCard(payload) {
  const data = payload || {};
  const components = Array.isArray(data.components) ? data.components : [];
  const rows = components.slice(0, 25).map((c) => {
    const estimator = c && (c.baseline_estimator || c.estimator);
    return [
      c && c.timestamp ? clock(c.timestamp) : '—',
      watts(c && c.residual_w),
      watts(c && c.baseline_w),
      watts(c && c.dynamic_w),
      watts(c && c.noise_w),
      estimator ? String(estimator) : '—',
      c && c.resolved === true ? 'Resolved' : 'Unresolved',
      label((c && c.kind) || 'unknown'),
    ];
  });
  const body = rows.length
    ? el('div', { class: 'table-wrap' }, [
        el('table', { class: 'data residual-decomposition' }, [
          el('thead', {}, el('tr', {}, [
            el('th', {}, 'Time'),
            el('th', { class: 'num' }, 'Residual'),
            el('th', { class: 'num' }, 'Baseline'),
            el('th', { class: 'num' }, 'Dynamic'),
            el('th', { class: 'num' }, 'Noise'),
            el('th', {}, 'Estimator'),
            el('th', {}, 'State'),
            el('th', {}, 'Kind'),
          ])),
          el('tbody', {},
            rows.map((cells) => el('tr', {}, cells.map((value) => el('td', {}, value))))),
        ]),
      ])
    : emptyState(
        'No residual observations',
        'The engine has not recorded a residual decomposition in this window.'
      );
  return card('Residual decomposition', [body], {
    foot: 'A signed measurement of what the engine could not attribute. Baseline, dynamic and noise are the residual’s own split — never redistributed to a device.',
  });
}

// GET /api/v1/mapping -> { cells: [...], coverage: { counts: {...} } }.
// Each cell states a device↔reader relation with its status, observability
// and contribution bounds.  A NON_IDENTIFIABLE cell carries bounds but no
// point contribution, so this never turns a shared conductor into a per-device
// watt.
function mappingMatrixCard(payload) {
  const data = payload || {};
  const cells = Array.isArray(data.cells) ? data.cells : [];
  const rows = cells.map((cell) => {
    const status = String((cell && cell.status) || 'unknown');
    const nonIdentifiable = status.toLowerCase() === 'non_identifiable';
    const estimate = cell && cell.estimated_contribution_w;
    const contribution = nonIdentifiable || estimate === null || estimate === undefined
      ? '—'
      : watts(estimate);
    const low = cell && cell.min_contribution_w;
    const high = cell && cell.max_contribution_w;
    const bounds =
      (low === null || low === undefined) && (high === null || high === undefined)
        ? '—'
        : `${watts(low)} – ${watts(high)}`;
    return [
      String((cell && cell.device_id) || '—'),
      String((cell && cell.reader_id) || '—'),
      badge(label(status), toneFor(status)),
      label((cell && cell.observability) || 'unknown'),
      contribution,
      bounds,
      label((cell && cell.relation_type) || 'unknown'),
      cell && cell.is_unknown_load ? badge('unknown load', 'warn') : null,
    ];
  });
  const coverage = (data.coverage && data.coverage.counts) || {};
  const byStatus = coverage.by_status || {};
  const summary = kv([
    ['Cells', String(coverage.cell_count === undefined ? cells.length : coverage.cell_count)],
    ['Unknown loads', String(coverage.unknown_load_count === undefined ? 0 : coverage.unknown_load_count)],
    ...Object.keys(byStatus).sort().map((key) => [label(key), String(byStatus[key])]),
  ]);
  const body = rows.length
    ? el('div', {}, [
        el('div', { class: 'table-wrap' }, [
          el('table', { class: 'data mapping-matrix' }, [
            el('thead', {}, el('tr', {}, [
              el('th', {}, 'Device'),
              el('th', {}, 'Reader'),
              el('th', {}, 'Status'),
              el('th', {}, 'Observability'),
              el('th', { class: 'num' }, 'Contribution'),
              el('th', { class: 'num' }, 'Bounds'),
              el('th', {}, 'Relation'),
              el('th', {}, 'Marker'),
            ])),
            el('tbody', {},
              rows.map((cellsRow) => el('tr', {}, cellsRow.map((value) => el('td', {}, value))))),
          ]),
        ]),
        el('div', { class: 'mapping-coverage', style: 'margin-top:14px' }, [
          el('div', { class: 'stat-label' }, 'Coverage'),
          summary,
        ]),
      ])
    : emptyState('No mapping cells', 'The engine has not derived a device/reader mapping matrix yet.');
  return card('Device-reader mapping', [body], {
    foot: 'A cell states how a reader observes a device and the bounds of its contribution. Non-identifiable cells carry a range, not an invented per-device watt.',
  });
}

// The /devices payload carries each result's min_w/max_w/group_members: the
// honest floor and cap of an inseparable group's SUM.  A device with such a
// range is non-identifiable on its own, so this renderer shows the group range
// and the members and NEVER reads the result's 0.0 ``power_w`` placeholder as a
// device measurement.
function groupRangesCard(devices) {
  const list = Array.isArray(devices) ? devices : [];
  const ranged = list.filter((d) => {
    const r = d && d.latest_result;
    if (!r) return false;
    const hasBounds =
      (r.min_w !== null && r.min_w !== undefined) ||
      (r.max_w !== null && r.max_w !== undefined);
    const hasMembers = Array.isArray(r.group_members) && r.group_members.length > 0;
    return hasBounds || hasMembers;
  });
  if (!ranged.length) return null;

  const rows = ranged.map((d) => {
    const r = d.latest_result;
    const range = `${watts(r.min_w)} – ${watts(r.max_w)}`;
    const members = Array.isArray(r.group_members) && r.group_members.length
      ? r.group_members.join(', ')
      : String((d && (d.name || d.device_id)) || 'device');
    return el('div', { class: 'group-range-row', dataset: { status: 'non_identifiable' } }, [
      el('span', { class: 'group-range-name' }, String((d && (d.name || d.device_id)) || 'device')),
      el('span', { class: 'chip chip-non-identifiable' }, 'NON-IDENTIFIABLE'),
      el('span', { class: 'group-range-value' }, range),
      el('span', { class: 'group-range-members' }, `Group total of: ${members}`),
    ]);
  });
  return card('Identifiability ranges', rows, {
    foot: 'A non-identifiable group is observable only as a total. The range is the group sum; no per-device watt is shown because the split is not measured.',
  });
}

/* --- Version history, suspected changes and event evidence ------------- *
 * Completion sprint B4-UIb (REQ-UI-009/010/011, gap G-09).  All three cards
 * are additive and read only fields the panel already receives from the API:
 * there is no new route.  Topology/config versions come from the current
 * /topology and /models payloads plus the version records mirrored into the
 * status/diagnostics bundles; suspected changes come from the audit bundle
 * (``status.audit.last_pass.configuration_changes``) and from the mirrored
 * ``configuration_change`` system events in diagnostics; event evidence comes
 * from the diagnostics event log and the isolation verdict list.
 *
 * A SUSPECTED change is NEVER shown as confirmed: its status badge is derived
 * only from the published ``status`` field, and a change whose status is not
 * literally ``confirmed`` is rendered with the SUSPECTED status and a "not
 * applied" marker.  Nothing here mutates configuration.
 * ---------------------------------------------------------------------- */

// A topology version record is the wire shape of TopologyVersion (contract
// 2.4.0): version_number, status, created_at, edge_count, created_by, summary.
// The current /topology payload is an authoritative single-row history when
// a fuller version list is not published.  Accept either a bare array or the
// common {versions: [...]} envelope, and add the current topology when no
// record duplicates it.
function versionHistoryCard(topology, modelPayload, versionPayload) {
  const versions = [];
  const seen = new Set();
  const add = (record) => {
    if (!record || typeof record !== 'object') return;
    const id =
      record.version_id !== undefined && record.version_id !== null
        ? String(record.version_id)
        : record.version_number !== undefined && record.version_number !== null
          ? `v${record.version_number}`
          : null;
    if (id !== null && seen.has(id)) return;
    if (id !== null) seen.add(id);
    versions.push(record);
  };

  const bundle = versionPayload || {};
  const records = Array.isArray(bundle)
    ? bundle
    : Array.isArray(bundle.versions)
      ? bundle.versions
      : Array.isArray(bundle.topology_versions)
        ? bundle.topology_versions
        : [];
  records.forEach(add);
  if (topology && typeof topology === 'object' && (topology.version !== undefined || topology.status !== undefined)) {
    // Only add the live topology when the version list does not already carry
    // its version number, so the same version is never shown twice.
    const already = versions.some(
      (v) => v && v.version_number !== undefined && topology.version !== undefined
        && Number(v.version_number) === Number(topology.version)
    );
    if (!already) {
      add({
        version_number: topology.version,
        status: topology.status,
        created_at: topology.created_at,
        edge_count: topology.edge_count,
        created_by: topology.created_by,
        summary: topology.summary,
      });
    }
  }

  // Model versions come from /models/{device_id}: {device_id, active, versions}.
  const models = [];
  const modelList = modelPayload || {};
  const deviceId =
    modelList.device_id || (topology && topology.device_id) || 'device';
  const activeVersion = modelList.active && modelList.active.version;
  (Array.isArray(modelList.versions) ? modelList.versions : []).forEach((model) => {
    if (!model || typeof model !== 'object') return;
    models.push(model);
  });
  if (!models.length && modelList.active) models.push(modelList.active);

  const topRows = versions.map((v) => [
    v.version_id !== undefined && v.version_id !== null
      ? String(v.version_id)
      : v.version_number !== undefined && v.version_number !== null
        ? `v${v.version_number}`
        : '—',
    v.version_number === undefined || v.version_number === null ? '—' : String(v.version_number),
    v.status ? statusBadge(v.status) : '—',
    v.edge_count === undefined || v.edge_count === null ? '—' : String(v.edge_count),
    v.created_at ? ago(v.created_at) : '—',
    v.created_by ? String(v.created_by) : '—',
    v.summary ? String(v.summary) : '—',
  ]);

  const modelRows = models.map((m) => [
    deviceId ? String(deviceId) : '—',
    m.model_type ? titleCase(m.model_type) : '—',
    m.version === undefined || m.version === null ? '—' : String(m.version),
    m.status ? statusBadge(m.status) : '—',
    m.confidence === undefined || m.confidence === null ? '—' : percent(m.confidence, 0),
    m.created_at ? ago(m.created_at) : '—',
    activeVersion !== undefined && activeVersion !== null && Number(m.version) === Number(activeVersion)
      ? badge('Active', 'ok')
      : null,
  ]);

  const table = (headings, rows) =>
    el('div', { class: 'table-wrap' }, [
      el('table', { class: 'data version-history' }, [
        el('thead', {}, el('tr', {}, headings.map((h) => el('th', {}, h)))),
        el('tbody', {}, rows.map((cells) =>
          el('tr', {}, cells.map((value) => el('td', {}, value === null ? '—' : value))))),
      ]),
    ]);

  const body = [];
  body.push(el('div', { class: 'stat-label' }, 'Topology versions'));
  body.push(
    topRows.length
      ? table(['Version', 'Number', 'Status', 'Edges', 'Created', 'By', 'Summary'], topRows)
      : el('p', { class: 'stat-hint' }, 'No topology version has been recorded yet.')
  );
  body.push(el('div', { class: 'stat-label', style: 'margin-top:14px' }, 'Model versions'));
  body.push(
    modelRows.length
      ? table(['Device', 'Type', 'Version', 'Status', 'Confidence', 'Created', 'Active'], modelRows)
      : el('p', { class: 'stat-hint' }, 'No learned model version is available.')
  );
  return card('Version history', body, {
    foot: 'Topology and model versions are append-only history. A candidate or suspected version is never treated as confirmed.',
  });
}

// A suspected configuration change is a ConfigurationChangeEvent (contract
// 2.4.0): kind, status ('suspected' | 'confirmed'), confidence, evidence_event_ids,
// occurred_at, device_id/reader_id.  It may arrive as the audit bundle's
// ``configuration_changes`` list or mirrored into a ``configuration_change``
// system event whose details carry the event under ``details.event``.  Neither
// shape is promoted: only a literal ``status === 'confirmed'`` reads confirmed,
// and even that is shown with its published status, never auto-confirmed here.
function suspectedChangesCard(status, diagnostics) {
  const changes = [];
  const add = (raw) => {
    if (!raw || typeof raw !== 'object') return;
    // The details envelope of a mirrored system event: {status, event}.
    const event = raw.event && typeof raw.event === 'object' ? raw.event : raw;
    if (!event.kind && !event.change_id) return;
    changes.push(event);
  };

  const audit = (status && status.audit) || {};
  const lastPass = audit.last_pass || {};
  (Array.isArray(lastPass.configuration_changes) ? lastPass.configuration_changes : []).forEach(add);

  const errors = (diagnostics && diagnostics.errors) || {};
  (Array.isArray(errors.recent_system_events) ? errors.recent_system_events : [])
    .filter((row) => row && row.category === 'configuration_change')
    .forEach((row) => {
      const details = row.details || row.details_json || {};
      add(details.event || details);
    });

  if (!changes.length) {
    return card('Suspected configuration changes', [
      emptyState(
        'No suspected changes',
        'The engine has not detected a configuration change that needs review.'
      ),
    ], {
      foot: 'A suspected change is an uncertain proposal and never applies itself. Only an operator can confirm a change.',
    });
  }

  const rows = changes.map((change) => {
    const statusValue = String(change.status || 'suspected').toLowerCase();
    const confirmed = statusValue === 'confirmed';
    // Deliberately NOT derived from detection/confidence: only the published
    // status decides, and a missing status stays suspected.
    const statusCell = confirmed
      ? statusBadge('confirmed')
      : el('span', { class: 'badge', dataset: { tone: 'warn' } }, [
          el('span', { class: 'dot', 'aria-hidden': 'true' }),
          'SUSPECTED',
        ]);
    const evidence = Array.isArray(change.evidence_event_ids)
      ? change.evidence_event_ids.length
      : 0;
    return el('div', { class: 'suspected-change-row', dataset: { status: confirmed ? 'confirmed' : 'suspected' } }, [
      el('span', { class: 'change-kind' }, change.kind ? titleCase(change.kind) : 'Unknown change'),
      statusCell,
      confirmed ? null : badge('not applied', 'mute'),
      el('span', { class: 'change-confidence' },
        `Confidence ${change.confidence === undefined || change.confidence === null ? '—' : percent(change.confidence, 0)}`),
      el('span', { class: 'change-evidence' }, `${evidence} evidence ${evidence === 1 ? 'event' : 'events'}`),
      el('span', { class: 'change-where' },
        [change.device_id, change.reader_id].filter(Boolean).join(' · ') || '—'),
      el('span', { class: 'change-when' }, change.occurred_at ? ago(change.occurred_at) : '—'),
    ]);
  });

  return card('Suspected configuration changes', rows, {
    foot: 'A SUSPECTED change is an uncertain proposal shown with its confidence and evidence count; it is never applied or auto-confirmed. Confirmation is an operator decision.',
  });
}

// Event evidence is drawn from two payloads the panel already fetches: the
// diagnostics event log (system/quality events, including configuration_change
// mirrors) and the isolation verdict list from /isolation.  A verdict shows its
// status, window bounds, competing device ids and reason code, as REQ-UI-009
// requires.  The two sources are labelled so they are never confused.
function eventEvidenceCard(diagnostics, isolation) {
  const entries = [];
  const errors = (diagnostics && diagnostics.errors) || {};
  const system = Array.isArray(errors.recent_system_events) ? errors.recent_system_events : [];
  const quality = Array.isArray(errors.recent_quality_events) ? errors.recent_quality_events : [];
  [...system.map((row) => ({ ...row, source: 'system' })),
   ...quality.map((row) => ({ ...row, source: 'quality' }))]
    .sort((a, b) => String(b.timestamp || b.created_at || '').localeCompare(String(a.timestamp || a.created_at || '')))
    .slice(0, 25)
    .forEach((row) => {
      const tag = row.category || row.code || row.level || row.severity || row.source;
      entries.push({
        when: row.timestamp || row.created_at,
        label: tag ? String(tag) : 'event',
        status: row.level || row.severity || null,
        detail: row.message || row.detail || '',
        evidence: null,
      });
    });

  const verdicts = (isolation && isolation.verdicts) || [];
  (Array.isArray(verdicts) ? verdicts : []).slice(0, 25).forEach((v) => {
    const competitors = Array.isArray(v.competing_device_ids) ? v.competing_device_ids : [];
    const window =
      v.window_start && v.window_end
        ? `${clock(v.window_start)} – ${clock(v.window_end)}`
        : '—';
    entries.push({
      when: v.occurred_at,
      label: v.device_id ? `Isolation · ${v.device_id}` : 'Isolation verdict',
      status: v.status,
      detail: [
        v.reason ? `Reason: ${v.reason}` : null,
        competitors.length ? `Competitors: ${competitors.join(', ')}` : null,
        v.power_window_contaminated ? 'Power window contaminated' : null,
      ].filter(Boolean).join(' · '),
      evidence: window,
    });
  });

  if (!entries.length) {
    return card('Event evidence', [
      emptyState('No event evidence', 'No recent events or isolation verdicts have been recorded yet.'),
    ], {
      foot: 'Evidence comes from the engine’s own event log and isolation analysis; nothing is inferred here.',
    });
  }

  const rows = entries.map((entry) =>
    el('div', { class: 'event-evidence-row' }, [
      el('span', { class: 'ee-when' }, entry.when ? ago(entry.when) : '—'),
      el('span', { class: 'ee-label' }, entry.label),
      entry.status ? statusBadge(String(entry.status).toLowerCase()) : null,
      entry.evidence ? el('span', { class: 'ee-window' }, `Window ${entry.evidence}`) : null,
      el('span', { class: 'ee-detail' }, entry.detail || '—'),
    ])
  );
  return card('Event evidence', rows, {
    foot: 'Engine event-log entries and isolation verdicts, newest first. A verdict names the window examined, competing devices and the reason code.',
  });
}

export {
  renderDevices,
  deviceSummary,
  deviceToolbar,
  paintDeviceGrid,
  deviceCard,
  renderDevice,
  deviceHero,
  deviceHistoryCard,
  sparkline,
  deviceEvidenceCard,
  deviceSourceCard,
  deviceTierCard,
  deviceModelCard,
  deviceTopologyCard,
  deviceConfigurationCard,
  shorten,
  copyExport,
  residualDecompositionCard,
  mappingMatrixCard,
  groupRangesCard,
  versionHistoryCard,
  suspectedChangesCard,
  eventEvidenceCard
};
