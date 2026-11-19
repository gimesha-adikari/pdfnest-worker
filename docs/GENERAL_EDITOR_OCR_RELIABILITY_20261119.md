# General Editor OCR reliability evidence

**Status — REAL-PDF OCR PARTIAL.** The original document completed OCR and the browser edit workflow on the candidate. AUTO language routing still omits script content on two verified pages.

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

## Prior release gate from the synthetic run

Qualification on the incident PDF previously required the owner's authorization and local file path. Run both worker revisions on the unchanged bytes in a private local test environment. Record only page sizes, timings and process metrics, verify extraction completeness and compare logical output on the affected pages. This correction is proposed for review. No production deployment or PDF upload is part of this evidence.

## Original-document qualification, November 19, 2026

The owner authorized private local processing of the original scan. This run used worker `dd09e64fcf753d95a47487fb877003128daf4b26` with the production SDK selector at `83132f538cd296ad292cb4b9627cb0ebf2d6237a`. [CI for the tested worker commit passed](https://github.com/gimesha-adikari/pdfnest-worker/actions/runs/44462658808). The browser submitted the original once to an isolated local frontend, Go API and Dramatiq worker using General Editor OCR V2, consumer `general_editor`, and AUTO English, Sinhala and Tamil. Kernel limits were one CPU and a 1 GB memory cgroup for the isolated actor and its children. The original and downloaded PDF digests, source and candidate files, page images and recognized text are kept in the restricted local evidence.

The source is a 4,385,962-byte, unencrypted PDF 1.7. `qpdf --check` found no syntax or stream errors. The document has 11 portrait A4 pages and two landscape A4 pages, all with 0° PDF rotation. Every page is dominated by a scan image and has a small visible native text layer. The SDK classified every page as mixed and used OCR. Page rasters at 200 DPI were 1653 × 2339 or 2339 × 1653, about 3.87 MP per page. Principal embedded scan images were approximately 1.8–2.3k pixels on the shorter edge and 3k on the longer edge. The document fits the editor's upload/page admission and 25 MP per-page raster bounds. Its private source SHA-256 matched before upload, in the frozen extraction baseline, and after verification.

| Measurement | Earlier evidence | Corrected worker on the original |
| --- | --- | --- |
| Native slot | Local admission wait expired at 30 s | Original admitted. Concurrent native fixture returned `EDITOR_OCR_BUSY` after 30.07 s of worker time while the original progressed |
| Completion | Production native execution deadline at 300 s | All 13 pages succeeded. Queue 0.292 s, worker 133.25 s, browser upload start to terminal result 134.83 s |
| OCR work | Earlier production trace stopped at the outer deadline | 55 language probes consumed 73.01 s and 13 final recognition calls consumed 29.41 s. The remaining worker time spans setup, rasterization, inter-stage work, normalization and result handling. The earlier failing production page/stage remains unknown |
| Resources | Production 1 vCPU and 1 GB limit | 130.57 aggregate CPU seconds, about 97% of one core over the measured browser window. Peak aggregate memory 613,425,152 bytes. Zero OOM, OOM kills or memory max events |
| Layout completeness | Unverified | 13/13 pages, 440 distinct line IDs and 2,702 usable word identities scoped to their pages. Page geometry matched the original, with zero out-of-page word boxes |
| Recovery | Previously tested on a synthetic workload | Next native extraction succeeded on the same actor in 1.26 s of worker time. Native slot reacquired without blocking, no native descendants remained, and Redis had zero active execution leases |

The old worker was not rerun on the private PDF. The earlier one-CPU controlled tests reproduced Tesseract oversubscription and isolated the thread cap as the reason the same synthetic work became faster. The original confirms the corrected path clears the previously observed deadline. Its corrected run shows repeated per-page language probes are the largest observed cost. It does not establish the precise inner stage that timed out in the historical production runs.

### Multilingual result

The original contains English, Sinhala and Tamil. The full AUTO result contains all three somewhere in the document, with incomplete page-level coverage. A separate explicit multilingual OCR sample supported investigation of two pages, followed by a two-page diagnostic with the same installed SDK. Confidence numbers below count characters in words scoring at least 70 on Tesseract's scale. They measure recognition and model coverage, not exact linguistic accuracy.

| Page | AUTO final recognition | Targeted explicit English/Sinhala/Tamil check |
| --- | --- | --- |
| 6 | English and Tamil. Zero Sinhala characters in the returned layout | Recovered 81 high-confidence Sinhala characters |
| 13 | English and Sinhala. Zero Tamil characters in the returned layout | Recovered 306 high-confidence Tamil characters |

Both AUTO decisions reported fused independent script evidence for the selected pair. Explicit trilingual recognition recovered the excluded script on the two pages without another full-document run. The evidence establishes a language-selection limitation separate from isolation and timeout reliability. It does not justify enabling a different language policy globally. Human transcription accuracy was not assessed.

### Browser edit and PDF integrity

The same extraction session rendered the original. One non-sensitive word was selected by its genuine frozen word ID and submitted in a `platen_word_edits.v1` operation. Compilation succeeded in 10.30 s of worker time. The verified preview loaded, and both local browser download controls completed. Worker artifact bytes, preview input bytes and the actual downloaded PDF shared one SHA-256. The original PDF was opened read-only.

The ordinary synthetic comparison helper did not account for a new logical word appended to a scanned region when unrelated native text already exists on that page. The real-document comparison instead checks the exact edited region, original native text outside that region, all original image streams and unchanged control pages. The mask remains the one declared from the frozen word geometry before compilation. The output passed qpdf, exact page-box and rotation checks, original image stream retention, and target-local MuPDF and Poppler text checks. All 78 page-render comparisons across both engines at 72, 144 and 288 DPI passed. There were zero changed pixels outside the mask and zero changed pixels on control pages under the established threshold and antialiasing policy.

### Regression and release position

A fresh focused worker run passed 55 tests, covering duplicate delivery, terminal states, capacity contention, cancellation, native wall deadlines, process group cleanup and recovery. No application source changed during the original-document qualification. PR #20 remains an unmerged correction for review. Original-document timeout recovery is verified, and complete real-PDF acceptance remains partial because of the demonstrated AUTO omissions.
