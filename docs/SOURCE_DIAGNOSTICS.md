# Private source diagnostics

The packet collector cannot reconstruct every request/response, and polling can
consume its event budget. Source diagnostics records the renderer's existing
decisions without adding backend calls, readiness waits, listeners or HTTP routes.
This is observation, not a playback fix. A confirmed backend transport/readiness
observation is not proof of physical audio output.

## Window and transport

One Diagnostics instance belongs to HttpApi and is shared by its listeners.
The existing backend monitor ticks one bounded off-loop file-I/O task. The normal
command path never waits for this task, storage, collection or upload.

The existing private hardware-test controller creates a strict 120-second lease
in cfg.state_dir/diagnostic-window.json. The native release's installation receipt
supplies the actual commit. Without a matching receipt/lease the recorder stays
inactive; there is no public debug endpoint or general debug switch. Lease JSON
contains exactly test_id, commit, started_at and expires_at. A source snapshot's
ready_at confirms activation; the user must act before expires_at, not assume
another 120 seconds remain after acknowledgement.

Sanitized snapshots are atomically published as diagnostic-buffer.json, mode 0600
under the existing state directory. No raw request, headers or body is copied.
The host copies a validated snapshot into the matching private recording before
a subsequent window can replace it. Collection reads preserved files, independent
of the renderer's process or ports. Partial snapshots survive a crash. A restarted
process does not overwrite a previous process's partial snapshot for the same
test ID; use a fresh window after collecting it. Expired buffers are removed at
the next observer pass after seven days, not by a new service.

## Schema 1

Each event contains schema_version, test_id, commit, software_version, UTC at,
a process-local request_id/last_request_id, protocol, fixed endpoint/action/
subcommand, a test-local peer pseudonym, stage, outcome, reason, HTTP/SOAP codes,
monotone duration_ms and generation/state before and after the request.
count/first_at/last_at describe compacted occurrences. The envelope also contains
the process identity, lease/ready/end timestamps, completeness and statistics.

Stages are command, backend, playback and state. Outcomes are accepted, rejected,
completed and unknown. Initial request_observed/unknown means a request arrived,
not that validation or playback succeeded. command_accepted comes from the existing
playback generation admission. Backend confirmations are emitted only at existing
transport/readiness confirmation points. State observations are not confirmations.
Unassociated background observations use protocol=backend and action=unknown;
they never borrow a nearby request. HTTP 200 alone is not an audio result.

Action labels come from checked SOAP contracts and fixed HTTP command sets/
namespace prefixes. Unknown client commands become unknown; their text is never
retained. Legacy namespace labels can be coarser than individual operations.
Web command POSTs use the same request context with protocol=http and fixed
endpoint=web_control. Only decoded actions from the browser's fixed schema become
labels; unknown/rejected undecodable commands remain unknown. Admission still runs
before dispatch, and the existing HTTP status and UNKNOWN result are preserved.
Assets, state polls, exports, radio search and output inspection are not traced.

Reasons are members of Reason or the existing QueueRejectionReason enum.
CreateQueue reuses its framing/queue/provider rejection reasons at their origin.
The existing SourcePlaybackError categories map to the fixed source_resolution,
source_unready and source_eof reasons. Unknown categories become unknown. Source
failure is recorded before existing cleanup; the final HTTP/SOAP/Web result keeps
that category instead of overwriting it with a generic backend failure. An
independent cleanup failure can still report backend failure. No exception text
is inspected. Readiness text logs contain neither URL nor backend snapshot.
Unclassified failures remain unknown; no exception-message parsing is used.

To add a new reason, add a fixed enum member in reviewed renderer code and call
the existing diagnostic note/failure helper at the decision point. The host
validates schema, types, identifier syntax, size and identity rather than keeping
a per-reason allowlist. New fields or schema versions require explicit integration
review; arbitrary extra fields are rejected. Enum-only construction in trusted
renderer code is the privacy boundary, not a general sanitizer for arbitrary text.

## Bounds and loss

The source quota is 1,000 records / 960 KiB snapshot plus up to 64 KiB host summary,
within the existing combined 5,000-record / 10 MiB event-data budget. Network
capture uses the remaining 4,000 records / 9 MiB; artifacts stay at 20 MiB.
Existing collector CPU/RAM/retention/privileges are not increased.

Only completed, known, successful read commands with unchanged generation/state
can compact. A read's observed start is removed only after success is known;
pending/error/unknown reads remain visible. Identical adjacent successful read
results share count/first-last time and request IDs. Mutations, errors and state
changes break aggregation. On pressure, completed read records are evicted first;
otherwise oldest records may be overwritten. dropped_events, compacted_events and
overwritten_events are separate. Overwrite counts logical occurrences, including
already compacted ones: do not add these counters as disjoint categories.

Memory, record count, serialized event and snapshot sizes are bounded. The writer
has at most one in-flight I/O task; no unbounded work queue. Diagnostic exceptions
never reject a command. Last unpublished data may be lost on crash or storage
failure. The host marks missing/invalid/mismatched/partial/lost data explicitly;
collection completion is not an app test pass.

## Targeted offline checks

    python -m pytest -q tests/test_source_diagnostics.py \
      tests/test_soap_faults.py::test_create_queue_diagnostic_is_request_local_and_402_only

These use real HTTP/SOAP/Web handlers with a synthetic backend and temporary
private files: rejected CreateQueue, accepted/confirmed and uncertain transport,
unknown commands, 5,000 BrowseQueue polls, pending reads, bounds, expiry and crash
snapshots. Two targeted cases additionally use actual ephemeral loopback HTTP
servers: one synthetic source returns 404, while a fake player acknowledges load,
requests that source once, and never exposes audio readiness. The real existing
readiness deadline is shortened only in the test. Web favorite_play and HTTPasp
setPlayerCmd:play each produce HTTP 502 / outcome=unknown / reason=source_unready /
state=ERROR with correlated acceptance and source-failure events. Fixed category
mapping, unknown-category fallback and privacy are checked for both boundaries.
No real mpv, device, external fetch, physical playback or app interaction is used.
The source's HTTP 404 is test evidence, not a new upstream-status diagnostic field;
source_unready alone does not distinguish 404, timeout or unsupported media.
The private hwtest PR tests schema validation and ZIP collection across this seam.
The synthetic queue-context-missing example is not a diagnosis of the Fritz issue.
