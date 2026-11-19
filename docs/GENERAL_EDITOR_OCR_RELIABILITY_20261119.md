# General Editor OCR reliability evidence

**Status: worker correction verified on synthetic scanned PDFs. Private incident PDF qualification is pending.**

## Observed incident

The local failure occurred before native execution. The legacy native boundary uses one filesystem lock per UID and temporary filesystem, with a 30-second admission wait. A concurrent extraction reproduced the reported capacity error after 30.02 seconds while the first job was still processing. Timeout, cancellation and recovery tests supported normal lock release. A leaked lock was not observed.

Railway production logs on November 19, 2026 showed repeated native execution deadlines. The active async worker ran `f6f4785e0200f0f4c229a32f61b65e918a82f299` on `main`, with one replica, one vCPU and a 1,000,000,000-byte container memory limit. Its General Editor selector was `sdk`, the native domain used the legacy default, and no OpenMP thread override was configured. The worker lock pins the SDK at `83132f538cd296ad292cb4b9627cb0ebf2d6237a`. The production logs establish expiry of the native supervisor's 300-second wall deadline. They do not identify the last page or OCR stage of the incident PDF.

## Workflow and measured mechanism

The General Editor uploads a PDF and polls the editor job. The Go API validates the upload, stores an owner-bound source, and sends a General Editor extraction to the worker. Dramatiq runs the extraction actor, checks cancellation and capacity, and starts the native supervisor. The child extracts trusted native text or renders each scanned page and invokes Tesseract. AUTO language detection probes the sampled page before final recognition, then the worker projects the layout and freezes its source-bound baseline. The actor records the final job state. The Go ownership check and existing browser error presentation carry the worker result to the caller.

For the dense synthetic workload, Tesseract 5.5.0 reached four threads even with `OMP_NUM_THREADS=1`. On one CPU, these threads consumed substantially more system time and completed much less OCR work before the supervisor deadline. Setting `OMP_THREAD_LIMIT=1` reduced the observed peak to one thread. The before and after one-page runs each made four probes and one final recognition call. The speedup preserved the detector's decisions, raster configuration and document geometry.

The fixture used 612 × 792 point Letter pages rendered at 200 DPI, yielding 1700 × 2200 pixels. The input was a JPEG-compressed raster containing 39 lines in three script blocks with Noto Sans, Noto Sans Sinhala and Noto Sans Tamil. Multi-page versions reused the image resource while still rendering and recognizing each page. The stage timer wrapped the actual Tesseract process, recording only the type of call, model names, elapsed time and process RSS. It did not record OCR output. The synthetic English, Sinhala and Tamil browser fixture used a separate page for each language.

| One-CPU test | Outcome |
| --- | --- |
| Internal engine, same dense page, baseline AUTO | 66.81 s total, 43.01 s in probes, 20.97 s in final recognition |
| Internal engine, same dense page, forced one-thread AUTO | 15.10 s total, 8.09 s in probes, 4.14 s in final recognition |
| Internal engine, same dense page, baseline explicit English | 12.69 s, one recognition call. Sinhala and Tamil output was outside the requested language |
| Internal engine, baseline 40-page dense workload | Native deadline in 301.08 s including termination, 4 pages completed. Tesseract was in the page 5 language probes |
| Internal engine, candidate eight-page dense workload | 8 pages in 115.51 s |
| SDK, baseline on the same eight-page source | Native deadline in 301.02 s including termination, 4 pages completed. The last command started was a page 5 language probe |
| SDK, candidate eight-page dense workload | 8 pages in 114.31 s |
| SDK, candidate with CPUQuota=100% and a 1 GB memory cgroup | 8 pages in 118.60 s. Peak aggregate memory 312,926,208 bytes, with zero cgroup OOM events |

The same CPU inefficiency can keep the local slot busy and exhaust the production wall deadline. This is a reproduced mechanism, not a private-document stage trace. Same-page AUTO on the deliberately dense trilingual synthetic selected English and Sinhala and missed Tamil in both variants. The patch did not change language routing. The separate-page browser fixture did return all three scripts.

## Correction and safety

- The native child sets `OMP_THREAD_LIMIT=1` before loading native OCR code. Both the internal and SDK call paths inherit it. Existing slot admission and the 300-second wall, 180-second CPU, 1 GiB child address-space, file and IPC ceilings are retained.
- Editor extraction uses the existing atomic job claim before acquiring the execution lease, preventing a duplicate delivery of the same job ID from starting parallel work. Separate owner-initiated submissions still create separate jobs.
- Redis admission refusal is recorded as a terminal state. The previous path raised Dramatiq `Retry` with `max_retries=0`, which could leave the application record queued without another attempt.
- Native slot wait expiry and execution timeout return separate actionable messages through the existing Editor status contract. Timeout diagnostics contain the stage, elapsed time and completed page count. Failed work publishes no layout. The cancellation and process group cleanup path remains in force.

## Regression evidence

- Four focused regression tests failed on the baseline and passed after the change. The final focused worker suite passed 185 tests, including Editor engine routing and language contracts, native timeout and cancellation cleanup, CPU and memory enforcement, concurrent capacity, recovery, legacy editing, Studio geometry and the word-edit baseline contract.
- `go test -count=1 -p 1 ./internal/edit ./internal/studio ./internal/worker` passed on backend `a4179d65099e3a8d11376923bf59af291eaac235` after supplying the existing test corpus in the isolated source snapshot. The frontend unit suite passed 148/148 files, its API-origin preflight passed 9/9, and `tsc --noEmit --pretty false` passed on frontend `134c2e264d68b22d3076add257f5ce8c68de25d2`.
- Playwright 1.61.1 and isolated Chromium 153.0.8010.12 exercised actual General Editor uploads to the clean frontend and Go sources, real Redis/Dramatiq and the worker candidate. The eight-page extraction succeeded. A concurrent native job received `EDITOR_OCR_BUSY` in the browser. A subsequent native file succeeded in the same worker run. English and three-page separate-script scanned fixtures extracted and displayed the editor. The three-page browser fixture also succeeded with the production SDK selector.
- A cancellation request made through the isolated job store while a browser-initiated extraction was running terminated the native process group, released the execution lease and produced a `cancelled` terminal state. The next accepted extraction succeeded without an actor restart. This is worker lifecycle evidence rather than a General Editor cancel UI feature.

## Separate observations

The missing `backend_durability_schema` relation reproduced in the isolated development database while the patched OCR pipeline still succeeded. The recovery-copy cleanup checks an explicitly versioned migration marker. Existing runbooks handle that migration separately. No database migration was required to reproduce or correct the native timeout mechanism.

Worker job metadata is stored with a TTL (default 86,400 seconds), and the worker status route returns 404 when the record does not exist. Forced expiry of an isolated job reproduced that route behavior. The cause of any particular historical 404 remains unverified.

## Release gate

Qualification on the incident PDF requires the owner's authorization and local file path. Run both worker revisions on the unchanged bytes in a private local test environment. Record only page sizes, timings and process metrics, verify extraction completeness and compare logical output on the affected pages. This correction is proposed for review. No production deployment or PDF upload is part of this evidence.
