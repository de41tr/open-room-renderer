# Saved DSP curve transfer

This Web workflow moves **one deliberately saved current-plugin EQ curve**
between existing rooms. It writes only the destination's saved preset library.
It does not load the curve, change active DSP, contact an audio backend, or copy
room identity, configuration, gain, source selection or account data.

## Export this context's curve

1. In Sound, select a saved preset. Apply or discard any local unsent band draft.
2. Choose **Export this context’s curve**. The preview contains only the scope,
   parameter count, size and whether the saved name has other stored contexts.
3. Review the privacy warning and explicitly consent to sending the original
   preset name and raw plugin identifier to this browser.
4. Choose **Prepare private curve file**, then deliberately click **Download
   JSON**. The fixed filename is `open-room-renderer-eq-curve.json`.

A shared preset can contain multiple plugin contexts. This operation exports
exactly its selected current-plugin curve. Other contexts are deliberately
omitted; this is not a whole-preset or whole-library backup. Built-in curves
must first be deliberately saved under a new safe name. A legacy unsafe,
redacted or noncanonical saved name must be explicitly renamed first. Errors
never reproduce the unsafe name or raw plugin identifier.

The ready link and a requested download do not prove a durable filesystem save.
Check the browser's downloads and keep the file private. Closing, Cancel,
Escape, page navigation, stale state or consent withdrawal release the local
Blob/link and private references. They cannot retract an already downloaded file.

## Import saved curve

1. Open the destination room's Web UI with confirmed DSP state, in the matching
   current plugin context. Apply or discard any local unsent band draft.
2. Choose **Import saved curve**, then select the local exported JSON file.
3. Review the redacted structural preview and explicitly consent to sending the
   file's original preset name, raw plugin identifier and curve to this renderer.
4. Choose **Import to library only**. Review the result before another action.

The name inside the file is preserved. It must be new across the destination's
entire saved library, including other plugin contexts. There is no overwrite,
automatic rename, merge or plugin remapping. A rare orphan active-name match
that would reinterpret the active curve's displayed name is also refused.
Select the imported preset and choose **Load** separately if you want to apply
it. Normal Load behavior enables EQ; importing does not.

Private source text is cleared on submission or dismissal. The UI never retries
a lost or unknown response, rebases a stale draft, or chains an import after a
DSP change. A changed library, active DSP context or renderer process makes old
drafts and prepared exports stale. A new explicit review is required.

## Exact format and limits

The document reuses `eq_presets.json` v1 representation:

```json
{"version":1,"presets":{"New saved name":{"exact-plugin-identifier":[
  {"param_name":"a_freq","value":30.0}
]}}}
```

The shortened array above illustrates structure only. A valid transfer requires
exactly forty records: every `a` through `j` band has `freq`, `gain`, `q` and
`mode`, once each. Record order and original numeric types are retained. No
missing fields are invented; no value is clamped, normalized or discarded.

- Exactly the top-level `version` and `presets` keys; version must be integer 1.
- Exactly one saved name, exactly one plugin and forty complete parameter
  records, each containing only `param_name` and `value`.
- The saved name is 1–256 UTF-8 bytes, nonblank, canonical, Web-safe and not
  `Flat`, `Bass Boost` or `Treble Boost`. URL/token-like names, including the
  existing conservative two decoding passes, are refused.
- The raw plugin is 1–8,192 UTF-8 bytes and nonblank. It is compared exactly to
  the current plugin. Both identifiers reject controls, C1 controls, surrogate
  code points, U+FFFE and U+FFFF; private plugin text is otherwise retained.
- Values must be JSON numbers, not strings or booleans. Duplicate fields,
  duplicate parameters, unsupported fields/contexts, invalid UTF-8, nonfinite
  numbers, overflow and decimal tokens that would silently round or underflow
  on decoding are refused.
- Existing supported ranges remain: frequency 10–24,000 Hz, gain −24–24 dB,
  Q 0.1–30, and mode −1, 0, 1, 2, 3 or 5.
- The file is bounded to 32,768 bytes. The **entire escaped JSON import request**
  must also fit 32,768 UTF-8 bytes. Export proves that exact request round trip
  using the original file text and a full-size DSP revision. A file below the
  standalone limit can still be refused because embedding escapes consume space.
- The resulting persisted saved library must fit its unchanged 1 MiB limit.
  A rejected addition does not truncate or overwrite the existing library.

The existing startup/legacy preset loader remains permissive and unchanged.
Transfer uses its own strict pure decoder with existing band validation; it
never validates by loading an uploaded document into the live library.

## Ownership, failure and privacy boundaries

Import and export use the existing renderer DSP lock and process-keyed revision.
Import directly calls `EqPresetLibrary.persist` for `eq_presets.json` only.
It never routes through `EQSave`, `EQSourceSave` or the helper that also writes
`active_dsp.json`. Active bands, plugin/source, enablement, balance, name caches,
active displayed name, gain and backend calls remain unchanged.

A failure before file replacement rolls back the saved-library proposal. A
failure after replacement preserves the committed in-memory library and returns
`UNKNOWN` with `LIBRARY_COMMITTED_DURABILITY_UNKNOWN`; visible readback cannot
prove disk durability. Do not automatically retry. The existing unavailable
storage latch blocks **both** transfer directions, even if a fallback or retained
mapping is present. Transfer never clears the latch, repairs storage, salvages
entries or exports a misleading empty fallback. This is startup failure
preservation, not detection of arbitrary later external file replacement races.

Prepare metadata and errors exclude original private names and plugin strings.
Downloading requires a fresh explicit disclosure flag, and importing requires
a fresh explicit file-disclosure flag. Export metadata is recomputed rather than
cached as a new authority. Existing same-origin Host/listener-port, CSRF,
Fetch-Metadata, bounded UTF-8 JSON and no-CORS checks apply unchanged. No new
listener, port, authentication, plugin authority or external service is added.
Legacy trusted-LAN controls remain unauthenticated.

## Verification scope

Focused pure codec, in-memory HTTP/MOCK and dependency-free JavaScript DOM-double
tests cover two independent rooms, exact boundaries, stale/restarted requests,
lock races, unavailable storage, no active/backend effects, failure rollback,
post-replacement uncertainty, consent and interrupted/repeated UI flows.

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_eq_transfer.py tests/test_web_dsp_transfer.py \
  tests/test_eq_preset_library.py tests/test_eq_preset_unavailable.py \
  tests/test_web_dsp_editor.py tests/test_web_playlist_export.py \
  tests/test_web_frontend.py
node --test tests/web_frontend.test.cjs tests/web_curve_transfer.test.cjs
```

These tests do not establish actual-browser or physical-audio acceptance for
this new workflow. The prior browser campaign is unchanged and was not repeated.
No new native campaign, AF_UNIX test lane or broader library redesign is needed
for this saved-only contract. Dedicated browser acceptance remains pending.
