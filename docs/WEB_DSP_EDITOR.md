# Local Web DSP editor protocol

This extends the existing same-origin `/orr/v1/web` boundary. It creates no
backend, source context, queue owner, persistence store, lock or polling task.
Legacy DSP command responses, lock ordering, rollback and cancellation shielding
remain unchanged. Browser admission is still a trusted-LAN safety boundary,
not authentication; the legacy endpoints remain unauthenticated.

## Confirmed projection

`GET /orr/v1/web/state` retains the existing DSP fields and adds `dsp.editor`.
It is `null` whenever DSP status is `UNKNOWN` or `PENDING`. The entire editor,
including the preset list, stays hidden while the DSP command lock is held;
partially staged active settings and library proposals are never displayed.
A confirmed editor has this shape:

```json
{
  "revision": "64 lowercase hexadecimal characters",
  "source": "global",
  "channel_mode": "Stereo",
  "bands": [
    {"param_name": "a_freq", "value": 30.0},
    {"param_name": "a_gain", "value": 0.0},
    {"param_name": "a_q", "value": 0.25},
    {"param_name": "a_mode", "value": 1.0}
  ],
  "presets": [
    {"id": "64 lowercase hexadecimal characters", "name": "Flat",
     "kind": "builtin", "shared": false}
  ]
}
```

The illustrative bands above are abbreviated: every real confirmed projection
and band-apply request contains exactly forty unique records, ordered `a` to
`j`, with `freq`, `gain`, `q`, `mode` for each band. Source is exactly `global`
or `wifi`, derived from the already active renderer context. Browser commands
cannot select a plugin URI, channel mode or source.

| Parameter | Accepted finite JSON number |
| --- | --- |
| `freq` | 10 through 24000 Hz |
| `gain` | -24 through +24 dB |
| `q` | 0.1 through 30 |
| `mode` | -1 bypass; 0 low shelf; 1 peak; 2 high shelf; 3 low pass; 5 high pass |

These are the existing EQ validator's limits, without clamping. Numeric
strings, booleans, duplicate parameters/JSON keys, unsupported modes, partial
curves and unknown fields are rejected. Zero gain does not bypass pass filters.

Preset names are private-safe display values; hidden unsafe names become
`Saved preset`. Raw plugin identifiers and raw token-bearing preset names are
never sent to the browser. Both revision and preset IDs are keyed HMAC views,
not reversible identifiers or unkeyed hashes. They are process-scoped and change
on restart. A preset ID is based on active plugin and original name; rename
changes it, content edits do not. The confirmed revision covers both the full
active DSP snapshot and the **entire** saved library.

Saved presets override same-name built-ins exactly as the existing library
resolves them. Deleting a legacy saved `Flat`, `Bass Boost` or `Treble Boost`
therefore reveals the built-in with the same ID and `kind: builtin`. Presence
checks after delete must check for a matching **saved** record.

## Strict commands

Each command is a same-origin JSON POST to `/orr/v1/web/command`, with the
existing CSRF header and admission rules. No other fields are accepted.

| `action` | Fields besides `action` |
| --- | --- |
| `dsp_bands` | `expected_dsp`, `bands` |
| `dsp_preset_load` | `expected_dsp`, `preset_id` |
| `dsp_preset_save` | `expected_dsp`, `name` |
| `dsp_preset_rename` | `expected_dsp`, `preset_id`, `name` |
| `dsp_preset_delete` | `expected_dsp`, `preset_id` |

`expected_dsp` is the revision from the snapshot on which the user's choice or
draft was based. Keep that original value with a dirty draft; do not silently
replace it with a newer poll's revision. Applying bands preserves EQ bypass.
Loading a preset enables EQ, following existing renderer semantics. Saving
copies the **confirmed active curve**, not unsubmitted browser values; disable
save while band edits remain dirty.

New names are trimmed, nonblank, at most 256 UTF-8 bytes, and must be safe to
display. Control characters, URL/token-like metadata (including escaped forms),
legacy `{Name=...}` reinterpretation and the three built-in names are rejected.
Save rejects any existing named library record, including one stored only for
an inactive plugin. Rename rejects a different already-existing name. There is
no implicit overwrite.

Rename/delete retain existing whole-named-preset semantics. A saved record
with `shared: true` contains curves for more than one stored plugin context.
The Web UI must explicitly warn and confirm that rename/delete affects all of
those stored contexts. The operation never silently changes to partial deletion.

## Locked admission and transaction ownership

`HttpApi._run_dsp_command(name, *, web_command=None)` retains its original
legacy execution path when the optional typed argument is absent. For a Web
editor request only, a synchronous hook runs **inside** the existing DSP lock,
after the existing restore reconciliation and before any transaction snapshot,
proposal, backend call or disk write. It checks complete confirmed readback,
compares the current HMAC, resolves the opaque ID in the current context, and
checks collisions. Only then does it build one fixed, safely quoted legacy DSP
command. Arbitrary legacy strings and browser-specified URIs cannot pass through.

The existing transaction retains all rollback, backend confirmation, library
commit and active-state persistence rules. Its shield keeps the lock held
through confirmation/rollback even if the browser request is cancelled.
After ordinary exceptions, the Web-only path captures effect classification
while still holding that lock. `CancelledError` and other non-`Exception`
failures retain their original behavior. Legacy exceptions are not wrapped.

A changed plugin, source, curve, balance, bypass, active name, or saved library
causes HTTP 409 `stale_dsp` with `outcome: REJECTED`; no effect has occurred.
Unknown DSP causes 409 `unavailable`, and a name collision is 409
`preset_conflict`. Read again and require the user to review their choice.
A stale guard never triggers an automatic retry or automatic draft rebasing.

## Acknowledgements and uncertain outcomes

Success remains `{"request_acknowledged": true}`. Read confirmed state again;
an acknowledgement is not a separate source of active/audible DSP truth.
Loading an already active curve may legitimately leave the revision unchanged.

An ordinary command failure returns HTTP 502, `error: command_failed`,
`outcome: UNKNOWN`, a fixed private-safe message, and one `effect`:

- `UNKNOWN`: the command failed without a stronger committed-effect claim.
- `BACKEND_APPLIED_PERSISTENCE_FAILED`: backend application was confirmed, but
  saving restart state failed with an `OSError`, including replacement with
  unconfirmed durability.
- `BACKEND_APPLIED_FOLLOWUP_UNKNOWN`: backend application was confirmed, but a
  subsequent non-storage follow-up failed; persistence may already have succeeded.
- `LIBRARY_COMMITTED_DURABILITY_UNKNOWN`: the library was replaced, but its
  directory durability was not confirmed; do not roll back the visible library.
- `LIBRARY_COMMITTED_ACTIVE_PERSISTENCE_FAILED`: the library write succeeded,
  but saving the resulting active DSP metadata failed.

For these fixed Web band/load paths, owned persistence is the only operation
that can raise `OSError` after backend confirmation. This closed-path assumption
supports the persistence-specific diagnosis; adding other post-apply I/O must
revisit it. Other post-confirmation exceptions remain a generic follow-up
failure. In particular, the existing legacy return-read parser can fail on a
literal percent escape in an active plugin identifier after both application
and persistence succeeded. The Web layer reports that truthfully without
changing legacy parser behavior or adding persistence state.

These classifications are not success acknowledgements. Never retry a mutation
on a 502, disconnect, timeout or lost response. Refresh and let the user make a
new explicit choice. An unconfirmed backend hides all active editor values.
The renderer may still report confirmed applied DSP after a persistence failure,
while the failure notice remains necessary.

## Focused verification

`tests/test_web_dsp_editor.py` covers real-loopback HTTP with a mock backend plus
unit parsing: complete snapshots, after-lock stale admission, source/plugin/
curve/library races, source and bypass preservation, cancellation shielding,
all persistence commit boundaries, private-safe IDs/names, saved built-in
shadowing, process-scoped IDs, and exact supported numeric/filter modes.
These tests do not establish native audio, browser rendering or physical output.

A local near-limit synthetic library (328 saved curves; 1,046,358 persisted
bytes) produced the complete editor projection in 8.82 ms median / 10.76 ms max
across 25 reads, with a 45,756-byte JSON editor. This short host-specific check
only rules out a material local polling cost; it is not a performance guarantee.
No cache or additional revision authority was added to obtain it.
