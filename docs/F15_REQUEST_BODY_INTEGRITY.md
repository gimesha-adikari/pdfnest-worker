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


## F-11 metadata integration qualification — 2026-10-27

**Draft source qualification only. F-11 and F-15 remain PARTIALLY RESOLVED /
OPEN; production activation remains NO-GO. PR #6 stays OPEN / DRAFT / UNMERGED.**

### Exact sources and preservation

- Previous draft: `aa820c3c2fe8aa166df7a9a74af6199aaef75299`.
- New main: `f074373fec05db5779281e1e0d45a58d67c6d0ce`, containing F-11
  render-session metadata isolation [#9](https://github.com/gimesha-adikari/pdfnest-worker/pull/9),
  fix `7d117b33ddb5fe703cc54a13b7afd0a440994367`.
- Normal non-force merge: `2bee15bd7de1bf16b1912b8b9d7e30afc7ab8da3`;
  first parent is the prior draft, second parent is new main. No textual conflicts.
- Backend sender: `53f42a3c7116063786c45a8dcdc39676abc31090`.
- Canonical docs reference: `c5ba5138b991888fd68c7e0fc7530b9c2a7ce0dc`.
- Main's exact post-merge [CI run 40542485083](https://github.com/gimesha-adikari/pdfnest-worker/actions/runs/40542485083)
  completed successfully before this refresh. This is source/build evidence only.

All render application files and `app/core/native_child.py` match new main.
F-14 actors/admission and F-16 Editor application files also match new main.
`app/core/security.py` and `app/main.py` match the previous strict draft: mandatory
five-field HMAC/raw-body binding, verification before dispatch, nonce protection,
128 MiB ceiling, 64 KiB chunks and four spool slots/process are unchanged.
The only additional changes beyond the merge are this append-only evidence,
a focused interaction test, and its inclusion in the existing strict-auth CI
step (with the already installed AnyIO plugin explicitly enabled). No new
application logic, dependency, protocol, limit or deployment switch was added.
The F-11/F-12, F-14, F-16 and non-publishing API/actor CI steps are retained.

### New interaction evidence

`tests/test_f15_render_session_integration.py` exercises the actual `app.main`
middleware stack and real render routes using loopback-addressed ASGI requests.
It does not start a listener or application lifespan/background services. Thirteen
cases serialize each small synthetic multipart once, hash those exact bytes,
and verify:

- Signed creation/deduplication, empty-body signed GET/page-query and DELETE,
  stored source equality, two-page metadata, and decoded JPEG dimensions.
- Missing/malformed hash, changed PDF bytes and invalid HMAC reject before
  metadata inspection/session creation and do not poison a valid nonce.
- Replay cannot recreate a deleted session; valid invalid/corrupt PDFs return
  safe `INVALID_DOCUMENT` errors without publishing a session.
- Real supervised probe children time out/crash, terminate descendants and are
  reaped. Authentication spools, private candidates and upload stages clean up;
  the next valid request succeeds.
- A real authenticated ASGI disconnect and direct request-task cancellation
  during active child processing stop/reap the child before removing the input.
  Auth spools are already closed after FastAPI consumes the verified multipart,
  while metadata work is still active; neither staging lifecycle owns the other.
- Native-capacity rejection remains safe HTTP 429 / `SESSION_METADATA_BUSY`,
  with recovery. Existing metadata/resource suites cover manager-lock availability,
  API responsiveness, atomic duplicate publication, deletion/eviction races,
  bounded render admission and repeated cancellation.

The child launch is redirected only in deterministic timeout/crash/cancellation
probes; normal cases execute the real isolated PDF metadata and page-render code.
No mocked authentication, HTTP parser, session manager or native-capacity policy
is used. The ASGI checks are not a claim of socket/proxy disconnect qualification.

### Focused local results

Python 3.12.13, installed published SDK 0.1.9; isolated temporary roots and
sequential native suites:

- Strict auth/body/API/gateway/sign/redact plus the 13 interactions: **207 passed**.
- Render/session/metadata/F-12 capacity/resource regressions: **59 passed**.
- Native isolation/subprocess/OCR API/markup preview: **55 passed**.
- F-14 Markdown claim/lease/limiter/conversion: **42 passed**, including a fresh
  disposable 32 MiB loopback Redis fixture; it was stopped by its fixture.
- F-16 Editor fidelity/geometry/OCR adapter regressions: **110 passed**.
- Current backend shared sender and independent AST package tests under
  `go test -race -count=1`: **28 passed, zero skipped/failed**, including real
  loopback Go HTTP to old and strict Python receivers for empty, JSON and
  streamed multipart bodies, plus tampered-body rejection.
- Changed Python syntax, frozen lock consistency, all 81 installed package
  compatibility, ancestry/source comparisons and diff integrity passed.

These suite counts overlap. Existing Starlette/httpx deprecation remains.
Initial new raw-ASGI probes used mixed-case header keys and were correctly
rejected; the test was corrected to the ASGI lowercase-header contract. An
initial Markdown run lacked the existing local Redis executable on PATH; the
properly provisioned disposable-fixture rerun passed. No application behavior
or security control was changed for either fixture correction.

### Queued-lifecycle reuse and remaining boundaries

The earlier actual Go HTTP -> strict receiver -> Redis/Dramatiq -> separate
actor -> native Markdown -> local artifact -> signed retrieval result above is
**reused, not rerun**. Exact Git blobs remain unchanged for actor/job models,
store/lease/broker/storage, submission routes, native operation registry,
isolation/supervisor/runner and all three queued-harness files. The new main
extracts `native_child.apply_resource_limits()` for metadata reuse; its mandatory
limits are preserved and the OCR protocol below `IN_CHILD` is structurally
identical. The 55 current native regressions and 42 admission regressions cover
that shared-helper integration. Backend worker/ocrv2/AST sender files also match
the previously qualified sender merge. Public Go API/PostgreSQL/billing/customer
retrieval and new full-queue execution are not claimed.

Current merged backend Studio PDF source admission is 50 MiB, and preview keeps
its distinct size-limit errors; historical 200 MiB notes above describe the prior
source only. Other workflows/object-reference transfers retain separate limits.
The authentication ceiling is unchanged. No deployed sender identity, shared
secret, replay-store availability, aggregate spool capacity, actor/Web API
transition or rollback readiness is established here. Reverting to the old
receiver would reopen F-15; stale hashless senders remain incompatible.

F-11 still does not establish aggregate host/process-tree or cross-node quotas,
a complete OS sandbox, or interruptibility of stalled filesystem I/O. GitHub
Codeac success is not a clean static-analysis verdict; introduced diagnostics
must be inspected separately. Exact refreshed-head CI/Codeac outcomes are recorded
on PR #6 after publication. No hosting access, manual deployment, production test,
PR merge, ready transition or auto-merge is authorized or performed.
