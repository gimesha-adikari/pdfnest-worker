# F-11 native execution-domain candidate — dormant, nonproduction, draft only

Starting main: `f074373fec05db5779281e1e0d45a58d67c6d0ce`.
Reference: [investigation #98](https://github.com/gimesha-adikari/platen-docs/blob/7122b420953bb0561da1056224bb481695bc6553/ai-handoffs/platen-antigravity-transition-2026-10-15/98-2026-10-28-F11-AGGREGATE-NATIVE-PROCESS-TREE-DECISION.md).
F-15 draft #6 at `084953ea7e4a72cd88201770894e524013ca4153` is unchanged.

## Configuration and actual delivery

Default/unset `NATIVE_EXECUTION_DOMAIN_MODE=legacy` keeps the existing process-group runner, flock admission, child limits, IPC and publication behavior. **Legacy does not contain arbitrary detached descendants.**

The only candidate selector is `NATIVE_EXECUTION_DOMAIN_MODE=required-nonproduction`. Unknown values fail closed. It is not enabled in application startup, Dockerfiles, CI runtime, production settings or deployment configuration. No production activation or merge is authorized.

**There is no real controller adapter, registration API, plugin loader, manager socket or fake-controller environment switch in this change.** The private registration is unset in shipped code. Therefore selecting required mode alone always refuses native execution; it does not produce hard-isolated OCR or metadata results. This is the explicitly permitted interface/source-test stopping point. Real hard-domain integration and kernel qualification are **BLOCKED** pending an independently controlled runtime.

Tests inject a test-only fake controller in process. A Python adapter can lie or hang; a dataclass receipt is not kernel proof. Do not register arbitrary/request-selected Python objects, reuse the F-07 experimental runner or treat fake-controller tests as runtime qualification.

## Shared contract and trust boundary

`app/core/native_domain.py` is shared by the existing native operation launcher and render-session metadata launcher only. The 24-operation registry, guardian, child limit bootstrap and subprocess runner are unchanged. Direct/persistent rendering, PowerPoint, Excel, Analyzer, redaction, signing and other native paths are outside this candidate.

Trusted future startup wiring must independently pin the controller's name, epoch, reviewed policy digest, finite slot set and exact resource limits. Capabilities must attest protected control-plane access, independent deadline/whole-domain kill, pre-native gating, aggregate accounting, bounded calls and reconciled recovery state. Policy changes during a registration are rejected. This source validates returned attestations and local invariants; only a verified runtime adapter can establish their truth.

The contract carries:

- Fresh operation nonce, finite slot identity, increasing generation and independent domain identity.
- Finite memory bytes, swap bytes (zero allowed), task count, CPU quota/period and wall time. The values must equal the pinned policy; this change does not approve new runtime budgets.
- `prepare`: create only a waiting trusted wrapper/member. Receipt must bind the exact lease and limits and state that native execution has not started.
- `run`: grant the verified gate, enforce its independent wall deadline, capture bounded output and poll authoritative cancellation/callback checks. No legacy fallback.
- `drain`: terminate the whole domain even after child success; verify emptiness, reaping and reconciliation against the exact lease/generation.
- `release`: commit FREE only after successful drainage, result validation and private staging cleanup. `quarantine` is addressed by the caller's fresh operation nonce, never an untrusted returned path/slot.

All controller methods receive absolute monotonic deadlines. A real adapter must enforce bounded control I/O and the independent watchdog outside native descendants; synchronous Python calls here cannot forcibly interrupt a broken controller. There is no thread-per-timeout workaround. The shipped unset registration refuses before any controller/child call.

The caller strips manager-bus/runtime-directory and candidate configuration variables from the hard launch environment. **That is not filesystem/socket confinement:** a future adapter must deny cgroup migration/administration, manager sockets, inherited control descriptors and equivalent aliases through a separately verified identity/access boundary. A same-UID user scope alone does not establish that trust. Current Worker containers are not assumed to have a usable user manager.

## Lifecycle and output ordering

The controller owns durable `FREE -> LEASED -> RUNNING -> DRAINING -> FREE` state; the caller mirrors it and checks local active slots/generations. FREE requires verified completion. Stale leases, changed identity/policy, wrong membership, early native execution, duplicate local slots, nonfinite/missing controls, incomplete drain or cleanup/release failure fail closed.

Hard launch ordering:

1. Verify capabilities and exact pinned identity/policy; acquire finite controller admission. The legacy flock is not used as proof of domain ownership.
2. Stage the existing request; prepare a gated member and validate its lease/limits before granting execution.
3. On completion, stop/reconcile descendants **before** reading/validating result frames and staged output. On cancellation/exception, attempt drain before deleting input/staging files.
4. Validate existing result/output contracts; stage a private output sibling; clean private request staging.
5. Recheck cancellation, release the verified controller lease, then atomically publish the pending output. A failure before this point cannot replace the previous output. Metadata returns no count/session until drainage, parsing and cleanup succeed.

If drainage cannot be established, retain the private directory for quarantine rather than delete files beneath live native processing. Ordinary test fixtures remove their own fake quarantine directories; the application has **no automatic cleanup/recovery implementation** for them. No public response/log adds the retained path.

This candidate conservatively latches the entire registration as blocked on an admitted failure, including cancellation, malformed results or uncertainty. No time-only reset or process-local "recover" API exists. Even a cleanly drained failed operation is quarantined; automatic reuse after such failures is deliberately unsupported. Subsequent normal operations are tested after verified successful cleanup, not after bypassing quarantine.

Recovery across real owner/controller death requires operator-owned durable state, independent deadlines and generation reconciliation. It is not provided by a parent flock closing, local Python memory, or restarting Worker. The future controller must refuse an unreconciled previous epoch before it can ever report recovery verified. Mixed legacy/hard populations do not automatically share one admission pool.

Repeated async cancellation in required mode joins the existing supervision thread through drainage. The legacy cancellation path is retained. Failure/capacity mappings reuse current contracts: native engine unavailable/timeout and metadata unavailable/busy; render-session HTTP uses existing 503/429 messages. No raw controller error is a public fallback.

## Qualification matrix — do not collapse these evidence categories

| Gate | Status / scope |
|---|---|
| Source identity, policy/limits, gate ordering, generation/slot checks | Unit-tested with deterministic fake controller; no kernel attestation claim |
| Missing controller / unknown mode | Unit and mounted HTTP refusal; no legacy child, session or output publication |
| Wrong membership, stale generation, pool full, controller error, quarantine failure | Unit-tested refusal and latch behavior |
| Known detached synthetic child on success/cancel/timeout | Real tiny child; test double kills/waits one known PID. Injected failure events; **not a general process-tree or kernel-domain controller** |
| Repeated async cancellation, callback exception, failed drain/cleanup/release, output preservation | Source/mounted-component tests with synchronization barriers and fake receipts |
| Owner death / restart | Simulated unverified-recovery refusal only; real independent watchdog/restart qualification **BLOCKED** |
| Aggregate memory/swap/tasks/CPU and cgroup migration resistance | Real controller absent; runtime qualification **BLOCKED**. No OOM/PID exhaustion tests |
| Existing default SDK/OCR, render, actor, Editor gates | Run separately; exact results in PR report/CI, not inferred from fake-controller tests |
| F-15 compatibility | Temporary local overlay of the unchanged F-15 draft and this candidate; no update to PR #6 |
| Real runtime gate / production activation | **BLOCKED / NOT QUALIFIED**; no runtime gate is silently skipped as a passing pytest test |

Focused CI runs the source-contract suite explicitly labeled **fake controller only**, alongside existing qualification steps and non-publishing API/actor builds. It does not enable required mode outside test injection or require new host services. A passing workflow is not a passing real execution-domain gate.

### Local qualification on this candidate

| Selection | Local result |
|---|---|
| Final source-contract / synthetic child / mounted metadata tests | 53 passed |
| Installed SDK 0.1.9 and F-11 security boundaries | 592 passed |
| Existing native OCR isolation/API/subprocess selection | 55 passed |
| F-12 raster/session/capacity/metadata selection | 59 passed |
| F-14 actor/lease/Markdown selection | 42 passed |
| F-16 Editor selection | 110 passed |
| Temporary F-15 strict-auth + current candidate overlay | 57 passed; PR #6 unchanged |

Runs used sanitized synthetic configuration and sequential transient user scopes with 1 GiB aggregate memory, zero swap, 64 tasks, one CPU-equivalent and finite manager/wall deadlines. The 1 GiB value is a ceiling, not an allocation test or proposed production policy. A disposable frozen-lock environment passed dependency checking with installed SDK 0.1.9. No resources in a production environment or existing development Redis were used.

Two environment/fixture failures were retained in local logs: the restricted PATH initially omitted the existing local Redis executable (19 passed / 23 fixture errors); adding that executable directory allowed the existing disposable 32 MiB loopback Redis fixture to pass. The first F-15 overlay run had 53 passes / 4 failures because a tighter inherited CPU limit conflicted with the retained probe's exact 180-second assertion. A standalone check reproduced the assertion; rerunning with that existing ceiling and unchanged outer resource bounds passed all 57. No test assertion, F-15 source or application limit was weakened. The final metadata helper keeps legacy JSON validation after its original capacity-release point; that ordering also has a focused regression.

The default source gates were run before that final legacy-order preservation adjustment; final source-contract and F-15-overlay runs include it. Exact draft-head CI requalifies the complete combined source. The pytest client emits an existing Starlette/httpx deprecation warning; no unrelated dependency upgrade was made. Local results and CI/container-build conclusions remain separate evidence.

## Independent review corrections — October 28, 2026

Review baseline: `c57248415df9c026556e4a066f6ea65d10bb33b3`. The two focused regressions failed on that source: a controller that allocated then raised `DomainBusy` escaped quarantine, and same-slot domain A -> B -> A was accepted on the third operation. Earlier qualification counts above describe that historical candidate, not this correction.

### F11-10-01: atomic refusal versus indeterminate acquisition

`acquire` now returns either a lease or an exact `BusyReceipt(identity, operation)` from the pinned trusted controller. A valid receipt attests an **atomic zero-side-effect decision for that fresh operation nonce**: no slot/domain/gate was allocated, and no controller state for the attempted acquisition changed. The receipt must match the exact identity, including epoch/policy, and the fresh nonce. Boolean values, lookalike/subclass receipts, malformed bindings and stale receipts are refused.

A bare exception from `acquire`, **including `DomainBusy`**, is indeterminate even when no lease was returned. The registration is latched blocked and quarantine is requested using the known fresh nonce; a failed quarantine call cannot clear that latch. No native child, staging or capacity reuse follows. Timeout/interrupted responses have the same rule. Caller cancellation is preserved after the quarantine attempt. There is no automatic retry, time-based reset or local recovery mechanism.

Only the validated zero-side-effect return is translated into the existing capacity result: native operation capacity timeout, or metadata `SESSION_METADATA_BUSY` / HTTP 429. It leaves the registration usable when capacity later becomes available. Uncertain acquisition maps to the existing safe unavailable result (native engine unavailable; metadata `SESSION_METADATA_UNAVAILABLE` / HTTP 503), without raw controller details.

**A Python dataclass or boolean cannot prove actual atomicity.** Receipt acceptance is a contract with a separately verified controller, not trust in an arbitrary Python object. The test double records synthetic before/after state; no real controller or kernel atomicity was added. A real controller must atomically commit either the refusal or a nonce-addressable lease, durably retain uncertain acquisition state, and reconcile it under independent authority. Calls require bounded transport and an external watchdog; source checks cannot interrupt a hung/malicious in-process adapter.

### F11-10-02: non-reusable acquisition identity

Every lease's domain must exactly equal `native-` followed by the lowercase SHA-256 of ASCII newline-joined controller name, epoch, policy digest, fresh operation nonce, slot and decimal generation (`domain_identity`). Validated tags contain no newline. This is a deterministic identity binding, **not a bearer capability or kernel proof**. The returned controller/nonce, finite slot and positive monotonic generation are independently checked. A valid domain for another nonce, slot, generation or controller cannot be substituted. Same-slot A -> B -> A and cross-slot identity reuse fail closed and quarantine before native work.

Local history stores only the highest generation for each pinned slot (at most 64 entries), not an unbounded history of domain strings. Successful subsequent operations receive new nonces and identities. A future real controller must independently guarantee durable domain non-reuse, nonce uniqueness/association and monotonic slot generations; it must reconcile active/quarantined leases and all previous epochs before asserting recovery. The fresh random nonce and process-local checks do not prove durable state after restart. Constructing a new `Registration`, changing an epoch or restarting Worker is **not recovery**. Tests simulate refusal against unreconciled controller state; they do not qualify actual owner-death recovery.

### Correction qualification

The expanded source-contract suite covers atomic busy state preservation, partial/exceptional admission (including timeout/interrupted replies), failed quarantine, stale/malformed refusals, nonce/controller/slot/generation substitution, A -> B -> A reuse, valid next leases, local registration replacement, epoch change, permanent latch behavior, and mounted 429/503 mapping. Existing callbacks, cancellation, output staging and legacy/default behavior remain covered. The test-only exception field is explicitly `Optional[BaseException]` and guarded with `is not None`; the Codeac diagnostic is addressed without suppression or weaker assertions. The equivalent union spelling triggered Codeac's `unsupported-binary-operation` diagnostic, so the annotation uses its compatible spelling.

Local correction results: **91 source-contract tests passed** (all prior 53 plus 38 new cases); installed SDK 0.1.9/security 592; native isolation 55; F-12 rendering/metadata 59; F-14 admission 42; F-16 Editor 110. The temporary unchanged-PR-#6 overlay passed all 57 strict-auth/render-session tests. The existing Starlette/httpx deprecation warning remains. Frozen-lock dependency checking, syntax parsing and diff checks passed. Runs used the same finite outer user-scope limits described above, disposable synthetic configuration and only the existing test-owned loopback Redis fixture. Exact-head GitHub Actions/container and Codeac results are recorded on PR #10 separately from these local results; a green Codeac status is not a clean static-analysis verdict.

Runtime integration, independent watchdog/restart authority and real kernel qualification remain **BLOCKED**. These corrections do not make the legacy default contain detached descendants and do not resolve F-11's operational resource-safety gap.

## Reproduction and next authorization

Use a clean Python 3.12 environment from the frozen lock, installed SDK 0.1.9 and synthetic `APP_ENV=test`/local storage configuration. Run `tests/test_native_execution_domain.py` under finite outer CPU, memory, task and wall limits. Tiny subprocess fixtures self-expire and are explicitly killed/waited. Do not use real documents, production environment files, shared Redis or hosted endpoints.

The next separately bounded task must select an operator-owned nonproduction controller and prove protected control access, pre-native membership, kernel controls, independent timeout/owner-death behavior, complete detached-tree drainage, durable quarantine/recovery and safe pool reuse. A user-level scope with an unrestricted bus accessible to native children is insufficient. No privilege, service provisioning, production budget, fleet admission or rollout is approved here.

F-11, F-07 and F-15 remain **PARTIALLY RESOLVED / OPEN**. F-15 production activation remains **NO-GO**. This candidate is **draft only; do not merge**.
