# task-12 — App UI wording

**Wave:** 6 · **Depends on:** task-09 · **Owner:** `addon/ui/app.js`, `addon/ui/app.css`, `tools/dev/check_panel.py`

## Objective

Remove every user-visible occurrence of "dedicated" from the panel and add a static gate that prevents regression.

## Context

Seven occurrences in `app.js` plus one CSS rule. The trap: deleting the `STRINGS.dedicated_device` key without an explicit alias makes the badge render `titleCase("dedicated_device")` = "Dedicated Device", a *different* string that is easy to miss in review.

The Circuits hint prints `0 dedicated` even on topologies with no dedicated rows — the visible zero-count leak.

Assets are served `no-store` (`addon/gateway.py:410-421`), so no caching concern.

## Scope

Delete every user-visible occurrence of "dedicated" from the panel and add a static gate that prevents regression.

The one deliberate exception is the `STRINGS.dedicated_device` alias key, which exists purely so the `titleCase` fallback cannot print "Dedicated Device". It contains the word but renders `Branch CT`. The static gate must allowlist exactly this line; see the check design in step 8.

## Files owned

- `addon/ui/app.js`
- `addon/ui/app.css`
- `tools/dev/check_panel.py`

## Steps

1. `STRINGS` (line 225): delete `dedicated: 'Dedicated CT'`; add `dedicated_device: 'Branch CT'` as an explicit legacy alias so the `titleCase` fallback can never produce "Dedicated Device".
2. `deviceTopologyCard` (line 1485): relabel `'Dedicated CTs'` → `'Monitored by'`; value from `topology.branch_ct_ids`.
3. `CT_TONES` (line 1533): map `dedicated_device` → `'info'` (the branch tone).
4. `topologySummary` (lines 1664-1694):
   - counts map becomes `{ main: 0, branch: 0, unknown: 0 }`;
   - circuit fallback `counts.branch + counts.unknown`;
   - hint `${counts.main} feed · ${counts.branch} branch · ${counts.unknown} unknown`.
5. Sort map (line 2314): `{ main: 0, branch: 1, unknown: 2 }`.
6. `deviceConfigurationCard` (line 1496): delete the Monitoring row. It is derived state, not a declaration: `derive_monitoring` resolves a non-`UNKNOWN` declaration first and falls back to the graph otherwise (`configuration.py:158-193`), so the row either repeats a stored value the user cannot edit here or restates the circuit graph. Keeping it would also imply a "dedicated" level the enum can no longer express.
7. `app.css:561`: delete the `[data-type="dedicated_device"]` swatch rule.
8. In `tools/dev/check_panel.py`, add a check that scans `addon/ui/app.js` for `/dedicat/i` and **fails unless every match is on the single allowlisted alias line** (`STRINGS.dedicated_device`). Implementation: collect matching line numbers, subtract the allowlist, and fail if any remain. This makes AC-1 enforceable without forbidding the compatibility key.

## Tests (implemented by task-14)

- Unit test invoking the new panel check against the real `app.js`: passes today, fails when a stray "Dedicated CT" string is introduced.
- A Python-side smoke check rendering the channels card's label function with a legacy payload shape and asserting the badge text is `Branch CT`. There is no JS runner in the repo, so AC-7 is satisfied statically rather than by a DOM render.

## Acceptance criteria

- AC-1 (UI part): no `/dedicat/i` in `addon/ui/**`.
- AC-7: the panel renders no dedicated text even on legacy data.
- The new static check fails on regression.
