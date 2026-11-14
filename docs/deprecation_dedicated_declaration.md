# Deprecation interface — `dedicated_device` declaration

**Status:** frozen for implementation · 2026-11-14
**Decision:** deprecate the *manual declaration*, not the *measurement capability*.

## What is deprecated

The requirement that a user declare `CTType.DEDICATED_DEVICE` (and a `device_id`) to obtain
direct measurement. The capability (`Source.DIRECT_MEASURED`, `resolve_direct()`,
`dedicated_channel_for()`) is **not** deprecated and must keep working unchanged.

## Why

- The declaration is stored three times with no precedence: `CTChannel.device_id`,
  `DeviceTopology.dedicated_ct_ids`, `DeviceConfiguration.monitored_by_ct_ids`.
- ADR-0026 already moved discovery to emit `branch`; "`dedicated_device` comes only from a
  declaration" — i.e. the only source is manual user input.
- The live install reports ~1032 W of `direct_measured` power, so deleting the capability
  today would silently downgrade measured watts (confidence base 0.97 → 0.88 or lower).

## Frozen interface

| Item | Value |
| --- | --- |
| Setting | `WM_DEPRECATE_DEDICATED_DECLARATION` (`deprecate_dedicated_declaration`), bool, default **True** |
| Setting semantics | `True` = emit deprecation notices; `False` = silence them. Never changes watts. |
| Warning code | `dedicated_declaration` |
| Log level | `WARNING`, once per `ct_id` per topology build |
| Diagnostics key | `health["deprecations"]` = list of objects (below) |
| Object schema | `{"code": "dedicated_declaration", "ct_id": str, "device_id": str|None, "since": "1.7.0", "replacement": str, "message": str}` |
| UI | Role remains selectable; help text marks it "(deprecated)". No option removed. |
| Behaviour change | **None.** Watts, source, tier and confidence must be byte-identical with the flag on or off. |

## Replacement path (documented, not auto-applied)

Declare the CT as `branch`, let circuit inference classify it, and when
`CircuitClass.DEDICATED` reaches `CONFIRMED`, that circuit is the evidenced equivalent of the
old declaration. **Do not auto-convert** existing declarations: conversion drops the direct
path today, so it must stay opt-in until the inferred path is wired as its replacement.

## Non-goals for this change

- Do not delete `CTType.DEDICATED_DEVICE`, `dedicated_ct_ids`, `Source.DIRECT_MEASURED`, or
  `resolve_direct()`.
- Do not change any inference output.
- Do not bump the contract version (additive diagnostics only).
