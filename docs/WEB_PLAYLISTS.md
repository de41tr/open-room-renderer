# Bounded Web playlist import and editing

The browser uses the existing guarded `POST /orr/v1/web/command` endpoint. The
renderer remains the sole owner of the canonical in-memory queue. This feature
adds no queue store, resolver, backend, export endpoint, or persistent queue.
An import or edit does not resolve or fetch a media URL, and never starts playback.
Only an explicit subsequent Play can do that.

## Strict request vocabulary

Each request has exactly the listed keys; `expected_generation` is the current
safe-integer playback generation from Web state:

- `playlist_replace`: `action`, `expected_generation`, `source`
- `playlist_append`: `action`, `expected_generation`, `urls`
- `playlist_remove`: `action`, `expected_generation`, `index`
- `playlist_move`: `action`, `expected_generation`, `from_index`, `to_index`

`source` is exactly one of:

- `{"format":"json","text":"<original authored JSON text>"}`
- `{"format":"m3u","text":"<original M3U text>","name":"Authored","index":1,"mode":"normal"}`
- `{"format":"urls","urls":["https://media.example.test/track.wav"],"name":"Authored","index":1,"mode":"normal"}`

The JSON variant preserves the authored document's name, index and mode. Its
original text is decoded by `decode_playlist`, retaining duplicate-field
rejection. M3U uses the strict offline `decode_m3u_urls` subset; it is not an HLS
manifest import or a URL resolver. Comments/EXTINF display text are discarded.
All imports use `explicit_queue`, including exact URL and escaped-request checks.
The five modes are `normal`, `repeat-one`, `repeat-all`, `shuffle-once`, and
`shuffle-repeat-all`. Positions are one-based; counts remain between 1 and 1,000.
New names are XML-safe, without leading/trailing whitespace or control characters,
and at most 256 UTF-8 bytes. Unknown keys, duplicate keys, booleans/floats in
integer fields, nonfinite numbers, invalid UTF-8 and invalid URLs are rejected.

The unchanged **32,768-byte whole Web request** limit includes the JSON envelope,
UTF-8 encoding, and JSON escaping. It is deliberately smaller than the independent
1 MiB authored-file and complete escaped SOAP limits reused by preparation.
Browser previews cannot bypass server validation. No input filenames or paths
are accepted by this endpoint.

## Admission and effects

All four operations require an already-confirmed stopped renderer: STOPPED phase,
stop intent, no retained backend identity, unlocked backend fence, and no pending
seek/load/native cleanup/staging/recovery/advance work or native uncertainty as
reported by the existing renderer fields. The Stop control is separate. An
unavailable request is rejected immediately, never queued to run after Stop.
This is a conservative first-release UX/scope choice, not a claimed core race
fix: the existing Stop path already checks generations after backend awaits.

Replacement accepts any stopped current queue, including an empty or provider
queue. Edits require a nonempty ordinary explicit queue. Provider source/search
identity, stream identity/type, item source/ID/type, radio resolver provenance or
attempts, and a differing resolved URL all make a queue unsupported for editing.
A played ordinary item's resolved URL may equal its explicit URL. Supported
canonical metadata, exact URLs, duplicate occurrences and selected occurrence
are preserved in detached copies; the Web implementation never uses the lossy
current-playlist export as an editing intermediate. A temporary read-only
validation view copies only the queue/list and items whose equal resolved URLs
need clearing, then validates the entire escaped request. No validation is
cached across polls, and the committed candidate retains those fields.

Append keeps the selected occurrence. Remove selects the following occurrence
when removing the selected item, or the final previous occurrence if removing
the last selected item; removing the only item is unavailable. Move uses final
one-based positions and follows the selected occurrence, including duplicates.
A same-position move remains a guarded replacement. Successful replacements use
existing `_replace_queue` behavior: generation advances, position resets, and
shuffle traversal restarts at the retained selection. No new lock ordering is
introduced. Final admission, detached preparation, and the stopped replacement
path have no intervening event-loop yield or hidden Stop.

## Safe state and outcomes

Web state adds `actions.playlist_replace`, `actions.playlist_append`,
`actions.playlist_remove`, and `actions.playlist_move` booleans. They combine the
stopped gate, structural eligibility, and count limits: append requires fewer
than 1,000 items; remove/move require more than one.

`queue.playlist_edit_reason` is a structural-only enum, independent of transport:
`"supported"`, `"empty_queue"`, or `"unsupported_queue"`. These fields disclose no
source URLs, file text, private metadata or resolver identity. State retains its
existing safe display projection; requests and responses never export sources.

Success returns only `{"request_acknowledged":true}`. Schema/preparation failures,
unavailability, and stale generations return a fixed `REJECTED` response before
side effects. A failure after dispatch is `UNKNOWN`, because the queue may already
have changed. Neither the browser nor endpoint retries a mutation automatically.
Read fresh state and make a new explicit choice after stale/unknown outcomes.
Raw input, exception messages and tokens are never included in these responses
or new logs. Existing Host/Origin/CSRF, same-origin, encoding/content-type, body
length, and bounded-read checks apply unchanged.

## Evidence boundary

`tests/test_web_playlists.py` covers pure preparation plus loopback HTTP with a
mock backend, including strict sources, metadata/duplicate selection, unsupported
queues, stopped/pending/uncertain gates, explicit Stop, stale repeated edits,
full escaped validation, envelope boundaries, no-fetch behavior and generic
post-dispatch failure. These tests are not browser execution or native audio
proof; those lanes must be reported separately.
