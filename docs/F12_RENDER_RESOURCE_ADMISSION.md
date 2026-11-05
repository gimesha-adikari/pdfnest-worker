# F-12 render resource admission

This source change supports one API process and one replica. There is no shared
render admission or session-accounting backend. At managed API startup (canary,
staging, production, using the configuration module's normalized environment),
`PDFNEST_RENDER_REPLICAS=1` is mandatory. Other values, multiprocess server
arguments, `WEB_CONCURRENCY` / `UVICORN_WORKERS` values other than one, and the
optional persistent render pool cause startup to fail. Persistent rendering
remains available for local tests; its memory/CPU lifecycle does not
satisfy this production contract.

The replica setting is an operator declaration, not provider discovery. Independent
filesystems cannot discover each other through a local file lock. Source tests do
not establish deployed topology or deployment-wide capacity. No hosting settings
are changed by this patch.

## Admission and storage

The existing render limiter admits at most two preview operations and bounds the
queue and execution time. Session metadata shares that limiter. Native metadata
and disposable page-render children both receive the preview hard ceilings:
512 MiB address space, 25 CPU seconds, and 64 MiB per output file. The existing
metadata native-domain admission remains in effect. These are per-child ceilings;
they do not reserve resources against unrelated worker jobs or certify a total
service memory limit.

Direct preview uploads are read in chunks with a 64 MiB maximum before native
work. Rendering reserves input bytes plus the maximum supported output (64 MiB)
before creating intermediates. Session renders reuse the already-accounted
source and reserve the maximum output. Sources, upload staging, render
intermediates, and pending deletion liabilities share atomic accounting under the
existing total 256 MiB budget. Owner-scoped session render outputs also count
against the 128 MiB owner budget. Configuration may lower those existing ceilings.
A small render can consequently be rejected when capacity cannot accommodate the
worst-case output; no image-size prediction is used to over-admit it.

Intermediates reside in generated private directories below the owned session
root. Cleanup releases a reservation only after physical removal is verified.
Failed cleanup retains the full reserved liability and is retried at later
admission. Restart holds the existing exclusive root lock, removes recognized
session/upload/render-job orphans, and fails closed on cleanup failure or
unexpected artifacts. No owner-wide sweep is introduced. Existing owner-only
session eviction and pending-session deletion accounting remain intact.
An active session render holds a source lease until its child is reaped. Delete,
expiry, and eviction hide the session immediately, while physical removal and
byte release wait for the last source lease. This prevents an unlinked, open PDF
from retaining disk blocks outside the accounting budget.

## Process lifecycle

A second process using the same root is rejected by its nonblocking file lock.
A process forked after application import is rejected before acquiring inherited
manager/limiter locks or accessing sessions. Disposable render and legacy metadata execution use an unprivileged guardian
that reuses the existing Linux pidfd and adopted-child reaper helpers. It does
not activate the privileged execution-domain controller. The guardian detects
API owner death, enforces a 30-second wall deadline, and drains all its adopted
children, including detached descendants, before publishing a dedicated pipe
receipt. Native children never inherit the receipt or cancellation descriptors.
The root flock's open description is inherited by both guardian and actual native
writer, so an API or guardian crash does not itself release writer ownership.

Both parent paths launch through the synchronous hardened Popen runner. It opens
and verifies the guardian pidfd before any poll/wait can reap that child. Parent
cancellation closes the control pipe, joins the work, and gives cleanup two
seconds. A stopped guardian is then terminated through its stable pidfd, followed
by a bounded half-second wait. These supervised modes never signal numeric
process groups after reaping a leader. Ordinary OCR runner behavior is preserved.

A missing post-reap receipt quarantines new render admission and retains source,
intermediate and reservation liabilities. Empty per-job active/drained journal
markers also make restart refuse an unresolved writer, even after its inherited
lock is gone. Startup may automatically reconcile a marked job only after the
actual guardian recorded successful drainage. No production mechanism fabricates
a receipt or clears quarantine. Unmarked artifacts from older code retain the
existing cleanup compatibility behavior; adopting an older root requires a
quiescent rollout, which this code-only change does not perform or attest.

API output reads are independently bounded. Resource ceilings and this trusted
guardian lifecycle are not a kernel security boundary against compromised native
code running under the same UID. This patch does not qualify F-11 containment,
filesystem byte quotas, shared admission, deployed topology, or production
behavior. No images are built locally and no service is deployed or restarted.

## Regression evidence

Negative regressions cover unsafe count declarations and server arguments,
missing production replica declaration, production persistent-pool rejection,
real inherited-manager fork use, real peer-process root exclusion, killed-owner
restart orphan recovery (including render intermediates), shared intermediate
budget concurrency and rejection before child launch, retained cleanup liability,
delete/expiry/eviction during a real child's open source read,
oversized direct uploads and outputs, native guardian owner death, an actual OS
file-size ceiling, repeated cancellation during subprocess creation, inherited
root ownership after API plus guardian death, missing-receipt quarantine,
restart refusal for unresolved writers, and bounded stopped-guardian cancellation
in both page rendering and metadata.

The existing owner-eviction, failed-delete, metadata/native failure, render raster,
capacity, timeout, and recovery regressions remain part of the focused gate.
Exact local commands, installed SDK provenance, test counts, and exact-head CI
results are recorded in the draft PR. CI/container results are distinct from local
source tests and do not authorize deployment.
