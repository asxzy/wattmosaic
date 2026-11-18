/* ==========================================================================
   WattMosaic Engine panel — view-diagnostics.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { ago, api, badge, card, clear, describeError, duration, el, get, kv, label, notice, notify, num, percent, post, render, state, statusBadge, titleCase } from './core.js';
import { eventEvidenceCard, suspectedChangesCard } from './view-devices.js';

/* --- Diagnostics ------------------------------------------------------ */

async function renderDiagnostics(root, signal) {
  // Additive B4-UIb evidence payloads.  Both are optional: an older engine that
  // does not serve /isolation (or a /status without an audit bundle) still gets
  // the full diagnostics view, with the evidence cards reading whatever the
  // diagnostics bundle already carries.
  const [report, isolation, status] = await Promise.all([
    get('/diagnostics', signal),
    get('/isolation', signal).catch(() => null),
    get('/status', signal).catch(() => null),
  ]);
  if (signal.aborted) return;

  root.append(
    el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('h1', {}, 'Diagnostics'),
        el('p', { class: 'lede' },
          'A redacted support bundle: what the engine is doing, what it rejected, and why. Safe to attach to an issue.'),
      ]),
      el('div', { class: 'head-actions' }, [
        el('button', { class: 'btn', type: 'button', onclick: () => downloadDiagnostics(report) }, 'Download JSON'),
        el('button', { class: 'btn btn-ghost', type: 'button', onclick: () => copyDiagnostics(report) }, 'Copy'),
      ]),
    ])
  );

  const health = report.engine_health || {};
  const db = report.database || {};
  const ha = report.home_assistant || {};
  const runtime = report.runtime || {};
  const models = report.models || {};

  root.append(
    el('div', { class: 'grid cols-2', style: 'margin-bottom:16px' }, [
      card('Engine', [
        kv([
          ['Status', statusBadge(health.status)],
          ['Version', health.engine_version || '—'],
          ['Contract', health.contract_version || report.contract_version || '—'],
          ['Uptime', duration(health.uptime_seconds)],
          ['Learning', health.learning_active ? badge('Active', 'ok', { dot: true }) : badge('Paused', 'mute', { dot: true })],
          ['Last inference', ago(health.inference_last_run_at)],
          ['Last successful cycle', ago(report.last_successful_processing_at)],
          ['Generated', ago(report.generated_at)],
          ['Report OK', report.ok ? badge('Yes', 'ok') : badge('No', 'bad')],
        ]),
      ]),
      card('Home Assistant and storage', [
        kv([
          ['HA connected', ha.connected ? badge('Connected', 'ok', { dot: true }) : badge('Disconnected', 'warn', { dot: true })],
          ['HA state', label(ha.state)],
          ['Last event', ago(ha.last_event_at)],
          ['Reconnect attempt', String(ha.reconnect_attempt ?? '—')],
          ['Database', !db.available
            ? badge('Not configured', 'mute')
            : db.healthy === true
              ? badge('Healthy', 'ok')
              : db.healthy === false
                ? badge('Unhealthy', 'bad')
                : badge('Unknown', 'mute')],
          ['Schema current', db.schema_current ? 'Yes' : 'No'],
          ['Models learned', String(models.count ?? 0)],
        ]),
      ]),
    ])
  );

  root.append(modelsTable(models));
  root.append(channelHealthCard(report.channels || {}, report.aligner || {}));
  root.append(errorsCard(report.errors || {}));

  // Additive B4-UIb: suspected changes (from the status audit bundle and the
  // diagnostics event log) and the event evidence list (event log + isolation
  // verdicts).  All fields come from the payloads above.
  root.append(suspectedChangesCard(status, report));
  root.append(eventEvidenceCard(report, isolation));

  root.append(
    detailsPanel('Configuration (redacted)', [
      el('pre', { class: 'code' }, JSON.stringify(report.configuration || {}, null, 2)),
    ])
  );
  root.append(
    detailsPanel('Runtime counters', [
      el('pre', { class: 'code' }, JSON.stringify(runtime, null, 2)),
    ])
  );
  root.append(
    detailsPanel('Full support bundle', [
      el('p', { class: 'stat-hint', style: 'margin-bottom:10px' },
        'The complete response from /api/v1/diagnostics, already redacted by the engine.'),
      el('pre', { class: 'code' }, JSON.stringify(report, null, 2)),
    ])
  );

  // Destructive admin action lives at the bottom of Diagnostics, past the
  // read-only support data, so it cannot be reached by a stray click.
  root.append(resetDatabaseCard());
}

/* --- Database reset (destructive) -------------------------------------- */
/* RESET_UI_BLOCK_START */
//: A true fresh start wipes ALL app data.  The panel offers no other undo, so
//: the action is guarded twice: the control sits at the end of Diagnostics, and
//: the dialog requires the operator to TYPE ``RESET`` before confirm is enabled.
//: The engine takes a timestamped backup first and returns its path, which the
//: dialog shows on success.
const RESET_CONFIRM_WORD = 'RESET';

function resetDatabaseCard() {
  return card(
    'Reset database',
    [
      notice(
        'Danger: this erases all app data',
        'Learned models, topology history, events, device state and configuration are all wiped. '
        + 'The engine writes a timestamped backup first; its path is shown after the reset.'
      ),
      el('div', { class: 'reset-card-actions' }, [
        el('button', {
          class: 'btn btn-danger',
          type: 'button',
          id: 'reset-db-button',
          onclick: openResetDatabaseDialog,
        }, 'Reset database'),
      ]),
    ],
    { foot: 'Destructive administrator action. A backup is created before any data is removed.' }
  );
}

function openResetDatabaseDialog() {
  // One dialog at a time — a second click on the card button is a no-op.
  if (document.getElementById('reset-db-dialog')) return null;

  const input = el('input', {
    class: 'reset-confirm-input',
    type: 'text',
    id: 'reset-db-confirm-input',
    autocomplete: 'off',
    placeholder: `Type ${RESET_CONFIRM_WORD}`,
    'aria-label': `Type ${RESET_CONFIRM_WORD} to confirm the reset`,
  });
  const confirmBtn = el('button', {
    class: 'btn btn-danger',
    type: 'button',
    id: 'reset-db-confirm',
    disabled: true,
    onclick: () => confirmResetDatabase(input, confirmBtn),
  }, 'Erase all data');
  const cancelBtn = el('button', {
    class: 'btn',
    type: 'button',
    id: 'reset-db-cancel',
    onclick: closeResetDatabaseDialog,
  }, 'Cancel');

  // Confirm stays disabled until the field holds the exact token.  A wrong
  // value (including trailing whitespace) never enables the destructive path.
  input.addEventListener('input', () => {
    confirmBtn.disabled = input.value !== RESET_CONFIRM_WORD;
  });

  const result = el('div', {
    class: 'reset-result',
    id: 'reset-db-dialog-result',
    role: 'status',
  });
  const dialog = el('dialog', { class: 'reset-dialog', id: 'reset-db-dialog' }, [
    el('h2', {}, 'Reset the database?'),
    notice(
      'This wipes ALL app data',
      'Every learned model, topology version, event, device record and saved setting is erased. '
      + 'This cannot be undone from the panel. A timestamped backup is created before anything is removed.'
    ),
    el('p', {}, `Type ${RESET_CONFIRM_WORD} to confirm:`),
    input,
    el('div', { class: 'reset-dialog-actions' }, [cancelBtn, confirmBtn]),
    result,
  ]);
  document.body.append(dialog);
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
  return dialog;
}

function closeResetDatabaseDialog() {
  const dialog = document.getElementById('reset-db-dialog');
  if (!dialog) return;
  if (typeof dialog.close === 'function') dialog.close();
  dialog.remove();
}

async function confirmResetDatabase(input, confirmBtn) {
  // Typed confirmation is mandatory even if a caller reaches this handler
  // directly (a disabled button is a visual guard, not the only one).
  if (!input || input.value !== RESET_CONFIRM_WORD) return;
  if (confirmBtn) confirmBtn.disabled = true;

  const result = document.getElementById('reset-db-dialog-result');
  const show = (node) => {
    if (!result) return;
    clear(result);
    result.append(node);
  };

  try {
    const data = await post('/commands/reset-db', { confirm: RESET_CONFIRM_WORD }) || {};
    const backup = data.backup_path || 'unknown';
    const schema = data.schema_version ?? '—';
    show(notice(
      'Database reset — backup created',
      `All app data was wiped. A timestamped backup was saved at ${backup}. `
      + `Schema version ${schema}. Reload to see the empty state.`,
      'ok'
    ));
    const done = el('button', {
      class: 'btn btn-primary',
      type: 'button',
      onclick: () => { closeResetDatabaseDialog(); render(); },
    }, 'Reload panel');
    if (result) result.append(done);
  } catch (err) {
    show(notice('Reset failed', describeError(err), 'bad'));
    if (confirmBtn) confirmBtn.disabled = false;
  }
}
/* RESET_UI_BLOCK_END */

function modelsTable(models) {
  const devices = models.devices || {};
  const rows = Object.entries(devices).map(([id, model]) =>
    el('tr', {}, [
      el('td', {}, [el('a', { href: `#/device/${encodeURIComponent(id)}` }, id)]),
      el('td', {}, titleCase(model.model_type || '—')),
      el('td', { class: 'num' }, String(model.version ?? '—')),
      el('td', {}, statusBadge(model.status)),
      el('td', { class: 'num' }, String(model.sample_count ?? '—')),
      el('td', { class: 'num' }, model.confidence === null || model.confidence === undefined ? '—' : percent(model.confidence, 0)),
    ])
  );
  return card(
    'Learned models',
    [
      rows.length
        ? el('div', { class: 'table-wrap' }, [
            el('table', { class: 'data' }, [
              el('thead', {}, el('tr', {}, [
                el('th', {}, 'Device'),
                el('th', {}, 'Type'),
                el('th', { class: 'num' }, 'Version'),
                el('th', {}, 'Status'),
                el('th', { class: 'num' }, 'Samples'),
                el('th', { class: 'num' }, 'Confidence'),
              ])),
              el('tbody', {}, rows),
            ]),
          ])
        : el('p', { class: 'stat-hint' }, 'No model has been learned yet. Learning needs repeated, validated cycles.'),
    ],
    { flush: true }
  );
}

function channelHealthCard(channels, aligner) {
  const entries = Object.entries(channels);
  return card(
    'Channel health',
    [
      el('p', { class: 'stat-hint', style: 'margin-bottom:10px' },
        `${aligner.channels ?? entries.length} channels tracked in the aligner`
        + (aligner.samples !== undefined ? ` · ${aligner.samples} samples buffered.` : '.')),
      entries.length
        ? el('div', { class: 'table-wrap' }, [
            el('table', { class: 'data' }, [
              el('thead', {}, el('tr', {}, [
                el('th', {}, 'Channel'),
                el('th', { class: 'num' }, 'Samples'),
                el('th', { class: 'num' }, 'Median interval'),
                el('th', {}, 'Last update'),
                el('th', {}, 'Quality'),
              ])),
              el('tbody', {},
                entries.slice(0, 40).map(([name, info]) => {
                  const data = info && typeof info === 'object' ? info : {};
                  // The aligner reports ``quality`` / ``last_received_at`` and
                  // ``last_update_age_s``; there is no ``status`` or
                  // ``last_seen`` field, and inventing one showed a table of
                  // em dashes on every installation.
                  const quality = data.quality || (data.missing ? 'missing' : data.stale ? 'stale' : null);
                  const age = data.last_update_age_s;
                  return el('tr', {}, [
                    el('td', {}, name),
                    el('td', { class: 'num' }, String(data.sample_count ?? '—')),
                    el('td', { class: 'num' }, data.interval_median_s === null || data.interval_median_s === undefined ? '—' : `${num(data.interval_median_s, 1)} s`),
                    el('td', {}, age === null || age === undefined ? ago(data.last_received_at) : `${duration(age)} ago`),
                    el('td', {}, quality ? statusBadge(quality) : '—'),
                  ]);
                })),
            ]),
          ])
        : el('p', { class: 'stat-hint' }, 'The aligner reported no channels yet.'),
    ],
    { flush: true }
  );
}

function errorsCard(errors) {
  const system = errors.recent_system_events || [];
  const quality = errors.recent_quality_events || [];
  const rows = [...system.map((row) => ({ ...row, kind: 'system' })), ...quality.map((row) => ({ ...row, kind: 'quality' }))]
    .sort((a, b) => String(b.timestamp || b.created_at || '').localeCompare(String(a.timestamp || a.created_at || '')))
    .slice(0, 25);

  return card(
    'Recent events',
    [
      rows.length
        ? el('div', { class: 'log-list' }, rows.map((row) => {
            const level = String(row.level || row.severity || 'info').toLowerCase();
            // System events carry ``category``; quality events carry ``code``
            // and ``channel_id``.
            const tag = row.category || row.code || row.kind;
            const where = row.channel_id ? ` (${row.channel_id})` : '';
            return el('div', { class: 'log-row' }, [
              el('span', { class: 'ts' }, ago(row.timestamp || row.created_at)),
              el('span', { class: 'lvl', dataset: { l: level } }, level.toUpperCase()),
              el('span', { class: 'msg' }, `${tag ? `[${tag}] ` : ''}${row.message || row.detail || JSON.stringify(row)}${where}`),
            ]);
          }))
        : el('p', { class: 'stat-hint' }, 'No recent system or quality events were recorded.'),
    ],
    { foot: 'Newest first, redacted by the engine before it leaves the process.' }
  );
}

function detailsPanel(title, body) {
  return el('details', { class: 'panel' }, [
    el('summary', {}, title),
    el('div', { class: 'panel-body' }, body),
  ]);
}

async function downloadDiagnostics(report) {
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: 'wattmosaic-diagnostics.json' });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function copyDiagnostics(report) {
  try {
    await navigator.clipboard.writeText(JSON.stringify(report, null, 2));
    notify('Diagnostics copied', 'The redacted bundle is on the clipboard.');
  } catch (err) {
    notify('Copy failed', describeError(err), 'bad');
  }
}

export {
  renderDiagnostics,
  RESET_CONFIRM_WORD,
  resetDatabaseCard,
  openResetDatabaseDialog,
  closeResetDatabaseDialog,
  confirmResetDatabase,
  modelsTable,
  channelHealthCard,
  errorsCard,
  detailsPanel,
  downloadDiagnostics,
  copyDiagnostics
};
