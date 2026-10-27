# Worker request-body integrity (F-15)

Protected HTTP requests use HMAC-SHA256 over the UTF-8 string
`METHOD\nPATH_AND_QUERY\nTIMESTAMP\nNONCE\nBODY_SHA256`.
`X-Worker-Body-Hash` is mandatory, lowercase, 64-character hexadecimal SHA-256
of the exact serialized HTTP body, including multipart boundaries and fields.
Empty bodies use SHA-256 of zero bytes. Hashless signatures are rejected.

The backend snapshots each serialized body before signing. Up to 64 KiB stays
in memory; larger bodies use private 0600 temporary files and 32 KiB copy chunks.
The Worker verifies headers first, then stages and hashes incoming bytes in
private unlinked files before dispatching any protected route. Replay to the
existing parser uses chunks of at most 64 KiB. Nonces are reserved only after
both the HMAC and actual body digest pass, with the existing Redis atomic NX
contract in managed environments. Invalid requests cannot consume valid nonces.

Both sides impose a 128 MiB raw-body envelope and five-minute staging deadline,
subject to shorter caller deadlines. This accommodates the normal 100 MiB file
budget plus multipart headroom. Configuring a route's file limit above this
envelope does not increase the authenticated raw-body limit.

Each process permits four disk spools (at most 512 MiB of authentication spool
data). These are per-process limits, not a host filesystem quota or global
concurrency bound. ASGI servers already own their incoming message buffers;
the middleware does not claim a bound on server/proxy buffers or total RSS.
Downstream parsers may have their own bounded temporary storage. Disk failures
fail closed; OS-level stalled disk I/O is not forcibly interruptible.

Source streams are closed on staging failure/cancellation to unblock multipart
pipe producers. Spools are closed/deleted on success, failure and cancellation.
The Worker waits for outstanding file I/O before closing its descriptor.
Explicit retries must build a new signed request with a fresh nonce; transport
replay of a previously signed body is disabled.

## Integration order

1. Integrate and deploy the backend signer, including the independent Python AST
   client and any operational callers. New signatures also work with the
   previous Worker (covered by a loopback Go-to-Python compatibility test).
2. Confirm every running sender uses the body-bound contract. A GitHub merge or
   passing CI does **not** establish which application instances are running.
3. Only then integrate the strict Worker verifier. Its PR must remain unmerged
   until this compatibility prerequisite is confirmed. There is no hashless
   compatibility fallback in this implementation.

The experimental gateway foundation remains inert. Its optional claim-only
upstream adapter is not qualified for strict-origin activation; it must not be
deployed as an alternative authenticated path.

Focused regressions cover raw JSON/multipart integrity, nonce protection,
bounded staging, parser replay, cancellation, cleanup and deterministic Go/Python
signatures. Cross-repository HTTP qualification is opt-in via `F15_WORKER_ROOT`
and `F15_PYTHON` when running `TestPythonReceiverHTTPCompatibility` in the backend.

## Draft refresh qualification — 2026-10-27

The refreshed candidate merges Worker main
`7b96c598ba4db28c16e77434097e1a26159593db` into the existing F-15 head
`91e58b50d3c61b92534e6bd6f41b920722d132e7` without rewriting either history.
The merge is conflict-free. The F-14 actor/admission tests and F-16 Editor/fidelity
tests match main; CI retains both qualification steps and the strict F-15 gate.
The refresh adds no application logic beyond the existing F-14/F-15/F-16
changes and leaves authentication limits and strict rejection unchanged. **PR #6 remains draft/unmerged; F-15 remains PARTIALLY RESOLVED /
OPEN.** Source qualification does not authorize activation.

Local synthetic qualification on the combined tree, Python 3.12.13 and installed
published SDK 0.1.9:

- Strict auth/body/multipart/API/gateway/sign/redact: 194 passed.
- F-14 Markdown admission/limiter/conversion: 42 passed, including disposable
  loopback Redis claim/lease races and a small real native conversion.
- F-16 Editor text/geometry/typography and related regressions: 110 passed.
- Native OCR/subprocess/API/markup compatibility: 55 passed.
- SDK/F-11 boundary CI gate: 592 passed, with no mandatory skips.
- Rendering/session/capacity, cancellation state and queued-reference contracts:
  34 passed with disposable loopback Redis. An initial run without that Redis
  fixture failed four cancellation-state tests; the correctly provisioned rerun
  passed. No application change was needed.
- Current merged backend sender `6713d7c26d95cdaadbebe6629aab0c43c35671c7`:
  race-enabled shared-client tests, including actual Go-to-Python loopback empty,
  JSON and streamed multipart requests against prior and strict receivers, passed;
  tampered bytes were rejected by the strict receiver. Independent Analyzer AST
  signing/error tests also passed under the race detector.

These are overlapping focused test runs, not a distinct-test total or a complete
HTTP-to-broker-to-actor-to-download qualification. Redis claim/state and native
operations were exercised, but a full backend/database/actor/storage queued-job
lifecycle was not run. Existing Starlette/httpx deprecation warning remains.

### Source-level body-size mapping (backend 6713d7c; Worker main plus F-15)

- **Studio initial/secondary source admission:** `internal/studio/source_upload.go`
  declares 200 MiB. Source persistence itself is a storage operation, not a
  Worker request. However `internal/studio/preview.go` resolves the stored asset
  and `defaultRenderWithWorker` streams the **whole source PDF** in an authenticated
  multipart request to `/api/v1/render/page`; it does not send only the chosen
  page. Thus a source that exceeds the 128 MiB raw-body envelope cannot use that
  path. The backend's current global Fiber request limit is also 100 MiB
  (`main.go`); the 200 MiB declaration alone does not prove such an upload is
  reachable through the default multipart API configuration.
- **Studio Editor/markup and queued OCR V2:**
  `internal/studio/job_coordinator.go`, `internal/edit/python.go`,
  `internal/markup/worker.go` and `internal/ocrv2/client.go:SubmitJob` submit JSON
  source/payload keys. Actors download those objects through storage, independently
  of authenticated HTTP body staging. The body hash authenticates these JSON
  references, not the subsequently downloaded object bytes.
- **Presigned storage:** `internal/storage/ownership.go:prepareOwnedUploads`
  accepts declared files up to 250 MiB for its allowed purposes. Remote presigned
  PUTs go directly to object storage (`internal/storage/r2.go:PresignUploads`),
  not through Worker authentication. That declaration is not evidence of a
  provider-enforced upload-size ceiling or of acceptance by later consumers.
  Reuse through the full-PDF Studio preview path still encounters the body cap.
- **Direct OCR/conversion/preview multipart calls:** the shared Go signer stages
  their complete serialized bodies, including multipart overhead, under the
  existing 128 MiB ceiling. Queued references are not a way to raise this ceiling
  for later direct transfers. No large-body/stress test was used for this mapping.

### Operational decisions still outstanding

All active senders, including deployed AST clients and retained operational
utilities, must be confirmed body-bound before strict Web API activation. This
source task has no deployment evidence. Historical Web API/actor version and
source-pin differences must be re-established only under separate authorization.
The actor does not enforce the HTTP middleware, but shared native/job changes and
queue/result compatibility mean coordinated version selection is still required.
Actor-first then Web API is a proposed sequence, not an approved rollout order.
Record both prior images/revisions, qualify queued jobs and resolve the size
contract before selecting that order. Stop on authentication/parser failures,
nonce-store errors, staging leaks/capacity regressions or queued-job incompatibility.
Rollback Web API to the recorded compatible receiver if authorized; this reopens
the original integrity weakness. Never restore a hashless sender while a strict
receiver remains active. No hosting access, deployment or rollout is authorized by
this draft refresh.

## Opt-in local queued lifecycle qualification — 2026-10-27

`tests/f15_queued_e2e.py` now qualifies the previously untested **private Go
client -> real Worker HTTP -> strict middleware -> real RedisBroker -> separate
Dramatiq actor -> native transformation -> local object -> signed retrieval**
chain. It does not qualify the public Go API/PostgreSQL/billing/customer-download
chain or establish production readiness. F-15 remains **PARTIALLY RESOLVED / OPEN**;
PR #6 must remain **OPEN / DRAFT / UNMERGED**.

Run from the candidate Worker root using its installed Python environment:

```sh
python tests/f15_queued_e2e.py \
  --backend-root /path/to/verified/backend-checkout \
  --redis-server /path/to/redis-server \
  --report /tmp/f15-queued-evidence.json
```

The backend must be at sender merge
`6713d7c26d95cdaadbebe6629aab0c43c35671c7`, with unchanged application files.
The test verifies that source identity, disables dotenv, passes only synthetic
configuration to child services and uses random loopback ports, a fresh 32 MiB
Redis instance/database, one actor process/thread and a private local storage
root. The Go fixture imports the actual backend `internal/ocrv2` and shared
`internal/worker` clients. Its temporary source directory inside the backend
module is removed after compilation. Existing development services are not used.
Dependencies must already be installed/cached; Go module network fetching is off.
An explicit `F15_GO_CACHE` development cache is optional; otherwise the cache is
also test-owned. This test is opt-in, not a new default-CI live-service requirement.

The two-page selectable-text `PDF_MARKDOWN_V2` fixture produces persisted
structured JSON containing Markdown. Evidence records real queue/message/job IDs,
actor PID, Redis `queued`/`running`/`succeeded` observations, timestamps, artifact
size/SHA-256 and process exits. Test-only middleware and an audit hook observe
real actor deliveries and native subprocess starts; they replace no application
function, broker, claim, storage or engine. Two enqueued deliveries of the same
job produce one native transformation, preserving the successful artifact.

Negative cases reject tampered bytes, missing/malformed hashes, invalid HMAC and
hashless signatures with HTTP 401, zero jobs and zero broker messages. A signed
status request succeeds once and rejects nonce replay. Result retrieval without
a signature fails. Missing objects and corrupt synthetic PDFs reach safe failed
states through the real actor, with no published result or internal path in the
public error; signed result retrieval is rejected.

The harness checks consumed broker payloads, released leases, removal of input,
processing and result-readback files, and absence of open unlinked auth spools.
The native subsystem's one persistent empty capacity-lock inode is expected:
its lock must be released, and it is removed only with the complete test-owned
root after shutdown. Redis and actor exit normally; the loopback Uvicorn process
exits on the requested SIGTERM. All test processes/descendants are stopped before
the temporary root is removed. Only the synthetic JSON evidence report is retained.

Local qualification passed the complete chain and 258 focused existing
security/Markdown-admission/output-contract regressions. Initial harness-only
launch/cleanup assertions were corrected (preserve Redis multicall executable
name; distinguish the expected fixed lock inode from leaked processing files).
No application correction was needed. Mid-native cancellation was **not tested**:
there is no externally signalled deterministic processing checkpoint in this
harness; no timing-dependent cancellation guarantee is claimed. Abrupt actor-host
failure, multi-replica duplicate races, production senders/storage, size-policy
reconciliation and the authorized actor/Web API rollout/rollback decision remain
outside this qualification.
