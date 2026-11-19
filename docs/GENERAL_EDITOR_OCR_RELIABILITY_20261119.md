# General Editor OCR reliability evidence

**Status — REAL-PDF OCR PARTIAL.** The latest AUTO correction recovered the two reported omissions. The independent OCR comparison found lower English and Tamil agreement on page 5, which blocks release qualification.

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

## AUTO minority script correction and original-document retest

**Verdict — REAL-PDF OCR PARTIAL.** The routing correction recovered the two reported omissions within the original resource limits. The independent OCR cross-check shows an accuracy tradeoff on another page. This draft is not qualified for release.

### Root cause and change

The SDK renders each scanned page, samples three bands and runs single-language Tesseract probes. The detector aggregates the probe mean confidence, script counts and whole-sample score, ranks the singles, then examines the best pair. The third script on page 6 (Sinhala) and page 13 (Tamil) had probe means of 39.46 and 29.77 despite well-recognized words. The preferred pair's score was 33.84 and 25.32 points higher, respectively. Both gaps exceeded the 25-point material-alt margin. The three-language check never ran. Final recognition therefore received a pair with one needed model absent. Normalization and word projection did not drop the script.

The SDK correction is `c80456de5aadba07826ec932c6f96c3d01cd1c6c` in [SDK PR #8](https://github.com/gimesha-adikari/platen-document/pull/8). The worker lock pins that commit and the worker's internal engine implements the same rule. A probe carries counts of high-confidence, single-script words, including combining marks. An otherwise accepted pair gains at most one confirmation probe when the excluded single already has three such words. The detector switches to all three models only if the combined probe confirms at least three words per supported script and matches or exceeds the pair's mean confidence and aggregate score. This branch uses the existing probe ceiling. A failed confirmation keeps the prior pair. Explicit language requests, native extraction, source binding, word identity format, isolation, thread limits, cancellation and the document deadline remain in force.

### Original-document result

The production SDK path completed 13/13 pages in **145.487 s worker time** and **146.832 s from browser upload to observed terminal result**. The previous timeout-only candidate took 133.25 s. The retest ran with one CPU, a 1 GB cgroup and the 300-second native deadline. It used 142.93 aggregate CPU seconds and peaked at 631,382,016 bytes with zero OOM events. The 58 probes took 82.57 s and 13 final recognitions took 31.95 s. The former candidate made 55 probes. The result had 437 lines and 2,658 usable word boxes, with matching page geometry and zero boxes outside their page. The private SHA-256 of the original remained unchanged and matched the frozen baseline. The source and output PDFs, original hashes, rendered pages and word records stayed in the private local environment.

| Pages | Languages used for recognition | Evidence |
| --- | --- | --- |
| 1 | English and Tamil | Latin output only. Word text and geometry identical to the preceding AUTO candidate |
| 2–3, 7–9 | English | Word text and geometry identical |
| 4 | English and Sinhala | Word text and geometry identical |
| 5–6 | English, Sinhala and Tamil | Sinhala recovered. Each page's complete word text and geometry matched its targeted explicit trilingual run |
| 10–12 | English, Sinhala and Tamil | Word text and geometry identical |
| 13 | English, Sinhala and Tamil | Tamil recovered. Complete word text and geometry matched the targeted explicit trilingual run |

A second comparison used a separate 220 DPI Poppler render, explicit trilingual recognition, and spatially matched words at confidence 85 or higher. Counts below are exact word-and-location matches, not a human transcription score.

| Page | English matches before → after | Sinhala matches before → after | Tamil matches before → after |
| --- | --- | --- | --- |
| 5 | 15 → 9 of 16 | 0 → 5 of 7 | 10 → 7 of 11 |
| 6 | 16 → 16 of 18 | 0 → 10 of 10 | 12 → 12 of 12 |
| 13 | 75 → 75 of 80 | 50 → 49 of 55 | 0 → 26 of 30 |

In the targeted 200 DPI SDK reference, pages 6 and 13 recovered 81 high-confidence Sinhala and 306 high-confidence Tamil characters, respectively. The confirmation probe improved page 5's sample score, but did not predict the drop in full-page English and Tamil agreement. This is a concrete remaining preservation gap. Exact agreement with explicit OCR does not establish linguistic accuracy, and the lower independent-reference agreement on page 5 prevents a full pass.

### Browser, recovery and tests

The same local browser uploaded, extracted and opened the original. A genuine baseline-bound `platen_word_edits.v1` request compiled, rendered a verified preview and completed both download controls. Artifact, preview and actual download SHA-256 matched. The candidate passed qpdf, MuPDF and Poppler target-local logical text, original image stream retention and page geometry checks. All 78 render comparisons at 72, 144 and 288 DPI passed with zero changed pixels outside the frozen mask and on untouched pages under the established policy. A concurrent job returned `EDITOR_OCR_BUSY` and the next native extraction succeeded on the same worker. Before the final rerun, cancellation of the earlier candidate released the slot and the waiting extraction succeeded.

Fresh local results were **443 SDK tests passed, 1 skipped**, and **182 focused worker tests passed**. The tests cover the minority decision, false-positive confirmation refusal, the probe budget, combining marks, Editor and Studio routing, word baselines, native timeout, cleanup and cancellation. Native, English-only scanned, separate-language scanned and same-page trilingual synthetic inputs also completed through the internal engine. No production deployment occurred.

## Page 5 recognition preservation and installed-SDK CI repair

### Diagnosis

The 200 DPI MuPDF diagnostic compared the previous pair and the full trilingual run on the same raster before SDK normalization and Editor projection. On page 5 the pair matched 15 of 16 Latin and 10 of 11 Tamil words in the independent 220 DPI Poppler OCR sample. The trilingual run matched 9 and 7 respectively. Changing among all six model orders yielded the same page 5 counts. A uniform-block segmentation diagnostic improved trilingual agreement to 12 and 8 and altered segmentation elsewhere. The observed loss originates in Tesseract's page-level recognition and segmentation. Sampling established the presence of a third script but gave no assurance about preservation of other words in the page recognition pass.

### Scoped correction

Only the AUTO route that has already accepted a pair and corroborated a third script runs the paired full-page recognition plus one combined recognition on the same prepared raster. The pair supplies the reading order, layout and existing words. A supplemental word must be entirely in the confirmed minority script after filtering punctuation, at or above the existing 70 confidence threshold, and align to consecutive weaker words on exactly one paired line. The merge checks meaningful box intersection, refuses partial overlaps or unanchored regions and prevents a second supplemental word from replacing an already accepted region. Remaining words retain the paired geometry and order. Token identities and line geometry are rebuilt consistently before normal validation. Explicit-language OCR, unconfirmed probes and other AUTO decisions retain their single-pass recognition behavior. Both passes share a deadline and use the existing capacity checks. The native supervisor's 300-second deadline, cancellation behavior and `OMP_THREAD_LIMIT=1` remain in force.

SDK `247cf13b06ccdc7d117a1b0c255f1f0c9a38d1c4` implements the rule and the worker mirrors it for its internal engine. The worker dependency and lockfile pin the SDK commit exactly. The failed CI run installed the intended newer SDK and then compared the lock to a hard-coded previous revision. The updated F-11 gate checks the full pinned revision and origin in the project, lock and installed distribution's Git provenance. It also checks the distribution version, import location and Python environment and still rejects mandatory skips. The local gate executed 604 tests, all passing. The focused worker suite passed 183 tests. The SDK suite passed 444 tests with one skip. The tests include weaker aligned spans, partial overlaps, stronger existing words, an unanchored supplemental word, explicit single-pass behavior, a non-corroborated script, probe ceilings, native cancellation and recovery.

### Real document

The original completed all 13 pages in **151.05 s from accepted browser submission to observed terminal status**, or **152.91 s including upload**. The actor and native children shared a one-CPU quota and a 1 GB cgroup. The cgroup used 149.14 CPU seconds and peaked at 660,983,808 bytes (630.36 MiB), with zero OOM events. There were 58 probe calls taking 80.95 s and 16 recognition calls taking 38.23 s. The three extra recognition calls were confined to the corroborated route. The preceding full trilingual candidate took 146.83 s including upload. The browser observed a competing extraction fail with `EDITOR_OCR_BUSY` after 31.33 s and a subsequent small extraction succeed in 2.41 s on the same worker.

The 220 DPI Poppler reference counted exact words at the corresponding position, with a 50% minimum overlap of the smaller box and Tesseract confidence of at least 85. The table includes the timeout-corrected pair baseline, the rejected whole-page trilingual revision and the new localized merge.

| Page | English pair → trilingual → localized | Sinhala pair → trilingual → localized | Tamil pair → trilingual → localized |
| --- | --- | --- | --- |
| 5 | 15 → 9 → 15 of 16 | 0 → 5 → 4 of 7 | 10 → 7 → 10 of 11 |
| 6 | 16 → 16 → 16 of 18 | 0 → 10 → 9 of 10 | 12 → 12 → 12 of 12 |
| 13 | 75 → 75 → 75 of 80 | 50 → 49 → 50 of 55 | 0 → 26 → 21 of 30 |

Every previously matched reference token retained the match at its original position, as checked individually on all three pages. All ten unaffected pages retained identical ordered word text and rectangles. The result contained 13 pages, 440 lines, 2,695 words with unique per-page identities and positive, in-page word geometry. The supplemental merge deliberately leaves some minority words unresolved when association is weak. These measurements qualify the recovered coverage and preservation against an independent render. They do not establish complete transcription accuracy.

The same browser session selected an authentic frozen baseline word and sent a digested, source-bound `platen_word_edits.v1` operation. The compile succeeded 9.81 s after submission. The candidate preview used the same PDF bytes as the worker artifact and the file saved by the two browser download controls. All three SHA-256 values matched privately. The original was unchanged and all document-specific data stayed local.

Fresh SDK CI passed 444 tests with one skip, built both distributions and passed 138 installed-wheel tests. [SDK CI run](https://github.com/gimesha-adikari/platen-document/actions/runs/44501304917)

The downloaded PDF passed `qpdf --check`, matched the source’s 13 page boxes and rotations, retained the original annotations and image streams, and preserved all original native words outside the frozen edit region. MuPDF and Poppler independently found the replacement at the intended location. The fixed source-derived mask was declared before compiling. All 78 visual comparisons at 72, 144 and 288 DPI passed with zero unauthorized changed pixels and zero changed pixels on untouched pages under the established threshold and antialias allowance. On the edited page, MuPDF measured 144, 449 and 1,439 in-mask changed pixels, and Poppler measured 135, 439 and 1,434. The task-owned native slot could be reacquired and no Tesseract or native supervisor process remained after the workflow.

The browser used Playwright 1.61.1 with isolated Chromium 153.0.8010.12 and the local frontend and Go API, without access to external destinations. The local Tesseract was 5.5.0, PyMuPDF 1.28.0, Poppler 26.05.0 and qpdf 12.3.2. The SDK and internal worker engine both completed the native, scanned English, separate-script scan and dense same-page trilingual fixtures.

The worker implementation is `de550c3ffb50725215527aaf6161c673263d9032`. The [worker PR checks](https://github.com/gimesha-adikari/pdfnest-worker/pull/20/checks) record the exact-head installed-SDK, security qualification and container-build results. No deployment or merge is part of this qualification.
