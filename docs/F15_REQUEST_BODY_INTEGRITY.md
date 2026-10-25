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
