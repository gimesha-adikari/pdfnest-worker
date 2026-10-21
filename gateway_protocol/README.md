# Inert gateway protocol foundation (M1-S2-58)

This package is not a gateway deployment. It has no HTTP listener, HTTP client,
entry point, environment-variable loader, production replay adapter, database,
operator endpoint or admission state. Nothing in `app/` imports it. All transport
implementations and credentials used in tests are synthetic. Existing worker,
backend, SDK, Dockerfiles, dependencies and workflows are unchanged.

## Inspected protocol and scope

Worker baseline: `931de889f7f72e071a433150db4bbc3fd892868e`.
Backend signing source: `f6e1c7853b9150c596ab87b89e184e424d2b6e1f`,
`internal/worker/client.go` (`SignRequestWithSecret`, `signingTransport`,
`CreateMultipartRequest`). The worker verifier is `app/core/security.py`.
Mounted routes were inspected from `app/main.py`, `app/jobs/router.py`,
`app/api/ocr_v2/{router,jobs}.py`, and all mounted `app/api/tools/*/router.py`.
The route-inventory regression reads these definitions without starting the app.
Codebase graph tools were unavailable; this was bounded direct-source inspection.

The backend streams multipart data through an `io.Pipe`, signs every request,
and uses a 30-minute client timeout. The worker has synchronous multipart routes,
JSON job submissions, streamed downloads, result/status retrieval, cancellation
and session cleanup. Profiles for structured extraction, searchable PDF and
Markdown also enter through the single OCR V2 jobs route. Studio/editor/markup
job submission and generic actor submission are processing. Rendering a session
page is a processing **GET**. Method alone cannot identify the admission plane.

No blocking incompatibility was found for these existing callers. The foundation
intentionally rejects noncanonical/ambiguous targets, duplicate headers, redirects,
and unknown routes, even while admission is open. It supports existing ASCII
route names and identifiers, including percent-encoded unreserved characters.
Encoded separators, dot segments, nested escapes, non-ASCII path components,
fragments, malformed escapes and repeated slashes are rejected rather than
normalized. Query spelling/order/repetition/percent escapes are preserved.
An eventual transport must prove the same behavior on actual HTTP/ASGI traffic.

## Authentication

`RequestTarget.canonical` reproduces the existing format:

```
UPPERCASE_METHOD\nDECODED_PATH[?UNCHANGED_RAW_QUERY]\nTIMESTAMP\nNONCE[\nBODY_HASH_HEADER]
```

HMAC-SHA256 is UTF-8 encoded and hex rendered. Header names remain the existing
`X-Worker-Signature`, `X-Worker-Timestamp`, `X-Worker-Nonce`, and optional
`X-Worker-Body-Hash`. This does not introduce a production authentication protocol.

Client and origin secrets must be supplied explicitly, be nonempty and distinct.
There is no development-secret fallback or production-secret access. Client
signatures cannot authenticate with the origin key. Forwarding strips client
signatures and creates a fresh origin timestamp/nonce/signature. The caller
cannot override the configured `FixedOrigin` through a target, Host, forwarded
headers or query parameter. Request/response objects redact their representations.
Errors use fixed messages and the helpers do not log requests or transport errors.

Authentication precedes classification, body iteration and the upstream attempt,
including control routes. Invalid signatures cannot reserve a nonce. A replay
adapter must atomically reserve a nonce and retain it through the entire possible
acceptance lifetime (twice the skew window plus one second), shared across every
instance using that client key. Missing, broken or inconclusive replay storage
fails closed. Only the tests provide an in-memory implementation: distributed
and persistent replay enforcement is **not implemented or qualified** here.

### Body integrity limitation

The worker signs an optional body-hash **claim** but never independently hashes
received body bytes. Tests demonstrate that different bodies with the same signed
claim both pass the real verifier. Neither a gateway signature nor that header
proves document-body integrity. A production transport must authenticate its peer
and protect both hops against modification and disclosure, normally with verified
TLS (or a separately reviewed authenticated encrypted private channel). Railway
private-network reachability alone is not evidence of that protection. Independently
verified end-to-end body integrity would require a separately reviewed protocol
change; it cannot be claimed from this foundation. No body buffering/hash scheme
or production authentication change is introduced here.

## Classification and admission

`routes.py` explicitly enumerates processing submissions and processing GETs.
Control routes are exact health/capability/language/metrics/docs endpoints and
bounded status, result, download, cancel and session-cleanup patterns. A successful
gateway authentication only permits forwarding: existing downstream authentication,
validation and ownership checks still run, and 4xx errors are returned unchanged.
The foundation does not assert those existing checks provide new tenant guarantees.
Status reads can have existing worker stale-job housekeeping effects; “control”
is an admission classification, not a promise of a side-effect-free read.

There is no maintenance-bypass header/query. `admission_open` is an explicit test
input. It is **not** an authoritative durable gate or an atomic check/register
operation. There are no active-work registrations or operator controls. This does
not resolve the old-worker synchronous-execution bootstrap/drain problem.

## Forwarding contract and limitations

`Forwarder` accepts only an injected `OneAttemptTransport` and `ResponseSink`.
No network transport is shipped. The fake transport proves helper behavior, not
real proxy, TLS, HTTP framing, load balancer or Railway behavior.

- Exact raw path/query and multipart bytes pass through without parsing/rebuilding.
- Header allowlists preserve content type/boundary, request correlation and existing
  owner identity. Cookie, Authorization, client signatures, forwarding/destination
  headers and hop-by-hop fields are not relayed. Connection-nominated headers and
  duplicates fail safely or are removed. Responses retain content encoding while
  bytes remain unchanged; no implicit decompression is allowed.
- Content length and transfer framing are not forwarded. A future HTTP adapter
  must frame streams correctly without buffering, preserve raw targets, disable
  redirects and **all** automatic retries, and have no ambient credentials/proxies.
- Each accepted request invokes `open_once` once. No redirect or failure causes a
  second attempt. Once the upstream call starts, transport/stream/sink failures
  are conservatively `AmbiguousUpstreamError`, including failures that might in
  fact have occurred before a byte was sent. Automatic resubmission is forbidden.
- Bytes are pulled one chunk at a time, at most 64 KiB per chunk, with awaited
  sink writes. Overlarge adapter chunks are rejected; the adapter must bound its
  own allocations. A lazy 64 MiB-per-direction test has less than 2 MiB traced
  Python peak memory. This does not bound native or external adapter memory.
- Request/response streams close on exit; the transport context must also release
  connections when opening fails before a response. Tests cover client disconnect,
  upstream failure, partial response, cancellation and cleanup failure.
- Task cancellation remains a `CancelledError` subtype, tagged with unknown
  upstream execution. It **does not prove cancellation of native work or jobs**.
  Repeated cancellation, process death, network half-close and asynchronous worker
  completion require later transport/durable-registration review. Response headers
  or partial bytes already sent cannot be recalled after an error.
- The fixed upstream is trusted to return legitimate response content. These
  helpers do not inspect/redact document response bodies. Secret headers are
  excluded and known credential values are not relayed in allowed headers.

## Qualification and independent review

Run locally (Python 3.12, existing test dependencies; no installs required):

```
APP_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
timeout 60s prlimit --as=1073741824 --cpu=45 -- /path/to/venv/bin/python \
-m pytest -q --tb=short -p no:cacheprovider \
tests/test_gateway_protocol.py tests/test_gateway_forwarding.py
```

Real-verifier compatibility tests invoke only `WorkerAuthMiddleware` with synthetic
Starlette requests and isolated settings. Multipart parsing uses the existing
Starlette parser against seven-byte fake chunks. No customer request, queue,
storage, database or production service is used by the new suites. Relevant
existing tests are `test_security.py`, `test_health.py` and
`test_ocr_v2_jobs_contract.py`. No dependency/environment installation is changed.

The existing CI explicitly selects F-11 files and does **not** select these new
suites. Its result verifies the unchanged existing qualification and nonpublishing
container builds, not execution of the new protocol tests. Wiring additional CI
coverage is deferred; no workflow change is included in this foundation.

Independent review is required before any persistent admission state, real HTTP
adapter/listener, credential/routing configuration, bootstrap or deployment work.
Production remains NO-GO. F-11 remains OPEN; native isolation, aggregate memory,
F-12/session rendering and PDF-to-PowerPoint boundaries are unchanged.

Local qualification on 2026-10-21 used the existing isolated Python 3.12.13
SDK-0.1.9 qualification environment (pytest 9.1.1, Starlette/FastAPI from the
worker dependency set), without modifying it:

| Check | Result | Resource bound / observed peak |
| --- | --- | --- |
| New protocol + forwarding suites | 135 passed, zero skips | 60 s wall / 45 s CPU / 1 GiB address space; 53,872 KiB maximum RSS |
| Existing security, health, OCR V2 job contracts | 20 passed, zero skips | 120 s wall / 90 s CPU / 1 GiB address space; 187,100 KiB maximum RSS |
| In-memory compilation and isolated-package dependency checks | 8 Python files passed | No bytecode, listener or service startup |

Existing tests emitted one upstream Starlette TestClient deprecation warning.
No broad native OCR/load suite, container build, dependency installation or
production processing was run locally. These are local observations, not
production or independent security-review acceptance.
