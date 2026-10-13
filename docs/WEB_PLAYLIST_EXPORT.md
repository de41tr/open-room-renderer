# Private queue JSON export

The Web controller offers a bounded, read-only export of the current queue.
It uses the existing version 1 authored-playlist JSON codec, preserving the
ordinary original source URL strings (with the proven-radio exception below),
their order and duplicates, queue name, selected
one-based index and all five queue modes. The server never saves an export file.

## Review, consent and download

1. Open **Export JSON** for a nonempty current queue. The dialog prepares a
   redacted summary: safe queue label, count, selection, mode and file size.
   Unsupported queues can be rejected at this point.
2. Read the privacy warning and select the initially unchecked consent checkbox.
   The file contains original source URLs and the original queue name. URLs may
   contain private tokens or signed links that expire. Keep the file private.
3. Select **Prepare private file** to request the private JSON bytes.
4. Once the dialog says **Private file ready**, select the genuine
   **Download JSON** link. No delayed response automatically triggers a download.
   The suggested filename is `open-room-renderer-playlist.json`.
5. Check the browser's downloads. A prepared Blob or a clicked link is not
   evidence that a file was saved, made private on disk, or durably backed up.

Close, Cancel, Escape, page navigation and a renderer restart discard this
flow's private browser references and revoke owned Blob URLs. A known queue or
session change makes the open review stale; reopen it and review again. Cleanup
cannot retract a file that the browser has already downloaded. As with the
other native dialogs, close the dialog before using background Stop controls.

The file omits titles, artists, artwork, durations, opaque metadata,
playback position/state, provider/account state, resolver provenance and shuffle
history. Export performs no media fetch, resolver request, or playability check.
An originally supplied temporary URL cannot reliably be recognized as such.
Successful syntax validation does not promise future playback.

## Supported queues and round-trip bound

Export is permitted while playing, paused, stopped, pending or uncertain; it
does not acquire playback or queue ownership, stop playback or change state.
It reuses `encode_current_playlist()` eligibility without adding the stricter
stopped-edit rules. Fully populated representable provider queues are lossy URL
snapshots, not provider/account backups.

Locally proven public-radio stations become canonical anonymous public station
references. Temporary resolved streams are never substituted for original URLs.
Every entry, including inactive entries, must pass the existing encoder's full
validation. Empty/unresolved provider queues, invalid URLs, unproven station
classifications and malformed or stale radio provenance reject the entire
export. No prefix, truncated list or silently omitted entry is returned.

Web export additionally requires the exact codec bytes to pass the existing
Web JSON import rules, including the 256-byte/control-character queue-name
restriction. The complete compact UTF-8 import request must fit the unchanged
32,768-byte request limit:

```json
{"action":"playlist_replace","expected_generation":9007199254740991,"source":{"format":"json","text":"<exact exported JSON text>"}}
```

The nested JSON text is escaped again, so a file smaller than 32,768 bytes can
still exceed the import request limit. The gate reserves the longest supported
generation value, regardless of the current generation. It never minifies the
codec output, rewrites URLs, or raises the existing request cap. Underlying
1,000-entry and 1 MiB authored/SOAP limits also remain in force. Some valid CLI
queues, or queues previously imported with a smaller generation envelope, can
fail this conservative Web export gate.

For supported larger queues or names outside the Web import rules, use the
existing CLI flow with a private destination you choose:

```sh
open-room-renderer playlist save-current current.json --host 127.0.0.1 --port 8080
open-room-renderer playlist restore current.json
```

The CLI's existing codec and eligibility limits still apply. See
[Getting started](../GETTING_STARTED.md) for private atomic/no-clobber CLI saving
and stopped-by-default restore. Browser downloads do not use the CLI
local file writer and do not promise its filesystem permissions or durability.
Favorites and M3U export are outside this feature.

## HTTP and consistency contract

`POST /orr/v1/web/playlist/export` is a separate private read, outside playback
command dispatch. It reuses the existing Host/Origin/CSRF checks, method and path
checks, query rejection, identity content encoding, JSON media type,
duplicate-header rejection, exact Content-Length framing, five-second bounded
body read and strict JSON decoding. This is a same-origin trusted-LAN boundary,
not authentication; existing listeners, legacy routes and CORS policy do not
change.

Prepare accepts exactly these keys:

```json
{"action":"prepare","expected_session":"<current Web session>","expected_generation":123}
```

Its response contains exactly these fields; `name` uses the existing safe
queue-label projection, never the original private-name fallback:

```json
{"version":1,"session_id":"<current Web session>","generation":123,"revision":"<64 lowercase hex HMAC>","name":"Current queue","count":2,"index":1,"mode":"normal","size_bytes":456,"filename":"open-room-renderer-playlist.json"}
```

Download accepts exactly these keys, with literal boolean `true` consent:

```json
{"action":"download","expected_session":"<current Web session>","expected_generation":123,"expected_export":"<revision from Prepare>","private_export_confirmed":true}
```

Generation is an exact, non-boolean integer from 0 through 9007199254740991.
The ASCII session string must match the current Web session exactly. Filenames,
paths, destination URLs, overwrite flags and other keys are rejected.

After reading the body, each request captures the current state and queue once.
There is no await between final session/generation validation, immutable codec
encoding, compatibility checks, HMAC verification and response construction.
A process-keyed, domain-separated HMAC-SHA256 covers the exact original codec
bytes. There is no server snapshot cache or export work during normal polling.

Download recomputes the document. A different session or generation rejects
it even when bytes match. Same-generation name, URL, mode, index and list changes
also invalidate it through the HMAC. Unexported metadata changes need not
invalidate it. The returned immutable bytes are a point-in-time document;
later queue changes cannot change an already returned file.

The download returns HTTP 200, `application/json`, exact Content-Length,
`Content-Disposition: attachment; filename="open-room-renderer-playlist.json"`
and all existing security/no-store headers. It has no command acknowledgement
wrapper. Prepare, state, favorites and error responses remain URL-redacted.
Errors never echo request values, source text or underlying exceptions:

- `export_unavailable`: HTTP 409, `REJECTED`; unsupported content or the stricter
  Web round-trip bound failed. The message points to the existing CLI.
- `stale_export`: HTTP 409, `REJECTED`; reopen Export and review the current queue.
- Existing admission errors retain their existing status and fixed schema.

## Verification boundary

`tests/test_web_playlist_export.py` exercises the synchronous codec gate and
in-memory HTTP handler with synthetic inputs. It covers exact byte round trips,
all modes, Unicode, duplicate occurrences, nested escaping and boundary sizes,
strict request/header/body admission, stale sessions/generations/same-generation
changes, fixed private-safe errors, provider/radio validation, and absence of
queue mutation, backend work, caching and polling serialization.

Existing Web admission and playlist tests also cover the reused boundary and
codec. Browser controller/DOM-double tests cover the interaction's request and
Blob lifecycle; those are not real browser acceptance.

The separate [real-Chrome smoke](WEB_BROWSER_ACCEPTANCE.md#reproducible-real-browser-smoke)
checks Blob preparation, explicit browser download and file bytes/filename,
export-dialog keyboard/focus, close cleanup, and severe browser/CSP errors on
desktop and mobile emulation. A successful exact-revision CI job and its artifacts
are required evidence; server or DOM-double tests never imply a successful save.
**BROWSER_ACCEPTANCE_PENDING** still applies to the wider manual matrix.
