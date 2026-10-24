# F-11 worker raster admission and SDK release integration

This draft is **not independently accepted or production-qualified**. Dependency
declarations and the frozen lock now select published `platen-document==0.1.9`.
The capability gate remains mandatory: incapable packages, including 0.1.8,
fail closed without fallback. The original source-candidate and actual-0.1.8
qualification below is historical evidence, not installed-0.1.9 evidence.

Verified source identities:

- Worker `main`: `9d38852e7ca1e7b657f7f644823d400553886ff0`.
- Historically reviewed SDK candidate: `729c6a121c66cbf439b06be488ffecb519ccdbd9`,
  [SDK draft PR 2](https://github.com/gimesha-adikari/platen-document/pull/2).
- Published SDK 0.1.9: `f7830972386a97bd0e8660f6cfc3596d83770f66`,
  tag `v0.1.9`, [release run 39450497079](https://github.com/gimesha-adikari/platen-document/actions/runs/39450497079), attempt 1.
- Backend `master`, reference only: `f6e1c7853b9150c596ab87b89e184e424d2b6e1f`.

## Enforced contract

`app/core/ocr_v2/geometry.py` carries the reviewed SDK estimator contract.
Every protected raster uses integer DPI 1–600, at most 10,000,000 conservative
pixels and 32,768 pixels on each axis. Default canonical OCR stays at 200 DPI.
An absent optional cap uses the hard ceiling; positive smaller caps survive;
larger caps are clamped. Boolean, fractional, nonfinite and nonpositive settings
raise `ConfigurationError`. An explicitly malformed structured raster environment
setting also fails instead of silently using a default.

Admission reads the finite, ordered, positive **visible rotated** `page.rect`;
it does not rotate it again or add the unrotated CropBox origin. A supported
clip is validated and intersected with that rectangle. Only the unshifted
uniform DPI transform is supported. Coordinates and transformed endpoints are
bounded to 1,048,576 to contain float/integer conversion uncertainty. Endpoint
floor/ceil rounding plus one guard pixel per edge gives conservative dimensions.
Axis and area checks use integer dimensions, including `height > limit // width`.

`render_page_pixmap` admits before `get_pixmap`, checks cancellation before and
immediately after native rendering, and checks observed dimensions before PNG
encoding, PIL conversion, language probes or OCR execution. Actual dimensions
must fit both the budget and the estimated bounds. A `ContextVar` carries smaller
budgets/cancellation through existing `prepare(page)` and fresh worker interfaces.
It is scoped/reset on exit; application code cannot raise the hard ceiling.
Worker native geometry compatibility mode and OCR routing remain unchanged.

`RasterAdmissionError` is document-fatal. Orchestration rethrows it and configuration
failures before the recoverable page-error catch. No complete result is returned
after a later page rejects. Earlier progress checkpoints may exist but do not
represent document success. Independently, structured projection retains a failed
OCR page as `FAILED` and marks its validation invalid, rather than fabricating a
`BLANK` or `SUCCESS` page from an empty element list.

## Raster sink inventory and reachability

This is a source inventory of the worker `app` tree, including supported SDK
selectors. Codebase graph tools were unavailable; direct source inspection and
literal sink searches were used. It does not assert that native dependencies
have no other allocations.

| Boundary / consumers | DPI and protection | Reachability / disposition |
| --- | --- | --- |
| `ocr_v2/geometry.py: RasterPreparer.prepare` | 200 default, shared preflight and observed-pixel guard | V2 text, searchable OCR, structured extraction, structured Markdown, V2 markup, editor/Studio helpers; default/fresh workers protected |
| `ocr_v2/structured.py` | smaller structured cap retained; historical 25m default now clamped to 10m | structured/document/editor/markup and PDF-to-Word/Markdown projections |
| `ocr_v2/renderers/searchable_pdf.py` metadata | admitted geometry at 72; **no pixmap** | `raster_width/height` remain, `raster_dimensions_estimated=true` added; upper estimates, not observed dimensions |
| `ocr_v2/renderers/validation.py` | source and output admitted independently at 72 **before either raster** | independent visual comparison remains mandatory; memoryviews avoid sample copies, native refs released in `finally` |
| `tools/ocr/document.py` | legacy OCR 300; AUTO preview 120 | legacy synchronous OCR and compatibility/job consumers; admission precedes PNG/PIL/probes |
| `tools/editor/document.py: ocr_words_for_page` | existing 2× scale = 144; validate scale/DPI | legacy editor and OCR-aware markup helpers |
| `tools/editor/document.py: sample_background_hex` | 72, validated/intersected clips | editor/markup background sampling; even clip geometry is validated |
| `tools/editor/document.py: compile_document` | existing 144; cancellation passed | Studio/editor compilation; page transform semantics preserved |
| `tools/markdown/service.py` | existing 150; explicit pixmap resolution set back to 150 | legacy Markdown fallback, before OCR; original PNG DPI semantics retained |
| `tools/pdf_to_office/converters/word.py` adaptive selection | maximum safe DPI ≤200, same estimator and effective structured cap | PDF-to-Word and shared Markdown page selectors; original intrinsic-image DPI selection retained; no safe 1 DPI => fatal rejection |
| `pdf_to_word_ocr_engine.py: _PageScopedRasterPreparer` | per-page selected integer DPI preserved; context cap reaches each fresh preparer | Word and Markdown internal/SDK extraction; selection and final admission agree |
| SDK 0.1.9 canonical/secondary renderers | same contract through `sdk_raster.py`, including smaller active budgets and cancellation translation | all 11 SDK factory families listed below; incapable 0.1.8 remains fail-closed |
| `tools/render/renderer.py` direct `get_pixmap` | **unchanged / not protected by this patch** | preview/session and shared preview renderer boundary; separate F-12/render-service work, not OCR admission |
| `tools/pdf_to_office/converters/powerpoint.py` direct `get_pixmap` | **unchanged / not protected by this patch** | separate non-OCR page-to-slide conversion resource boundary; outstanding, not an F-11 closure claim |

After correction the only direct `get_pixmap` calls in `app` are the shared
guard, the preview/session renderer and the PowerPoint converter. The latter
two are intentionally excluded, not classified as safe.

The 11 guarded SDK selector families are `ocr_text_engine`,
`document_extraction_engine`, `searchable_pdf_engine`, `ocr_markup_engine`
(including its independent preview constructor), `editor_ocr_engine`,
`legacy_editor_ocr_engine`, `legacy_markup_ocr_engine`,
`studio_editor_extraction_engine`, `studio_markup_region_ocr_engine`,
`pdf_to_word_ocr_engine` and `pdf_to_markdown_engine`. All processor construction
requires the raster contract before running SDK work. SDK typed raster/config
exceptions are translated to the worker's types. The bridge also translates
cancellation into the SDK's cancellation type and restores the original worker
exception at exit so visual validation cannot mislabel it as a PDF failure.
The internal validator preserves `JobCancelledException` directly.

Public entry points include internal V2 sync text/markup-preview routes, V2
durable jobs, legacy OCR, editor extraction/compile, legacy markup, Markdown,
and PDF-to-Word conversion. Existing ownership, billing, leases, routing and
backend job contracts are unchanged. V2 sync failures use existing `INVALID_INPUT`
(422) / `INVALID_CONFIGURATION` (500) envelopes. Job actors record terminal
failed state with the same existing codes and fixed safe messages; they do not
upload successful output or retry an admission failure. Compatibility routes
retain their existing error envelopes. Newly generated public errors contain no
document text or local input path.

## Artifact boundary

Searchable PDF writes into an owned sibling temporary file. Metadata admission
and independent text/image/visual validation must all succeed before `os.replace`
publishes it. A failure removes only that owned file and preserves any preexisting
destination. Validation still raster-compares source/output rather than replacing
visual proof with metadata. Text/structured/Markdown/markup job execution raises
before output upload on admission rejection; task-owned job cleanup remains in
the existing `finally` blocks. No unrelated destination is deleted.

## Historical source qualification and reproduction

This section records worker commit `b635cb66b3fb6e2003078d62acb0270fec2ed728`.
Its commands and old test names apply to that revision. Current installed-package
qualification is recorded separately below.

No installations, browser runs, large renders or parallel suites were used.
The existing worker virtualenv provided Python 3.12.13, pytest 9.1.1,
PyMuPDF 1.28.0 and Pillow 12.3.0. Tests run in this worker worktree with `PYTHONPATH=.`;
the candidate command appends only the clean SDK candidate `src` directory and
asserts the imported SDK module's exact path. Its HEAD and unchanged source were
verified against `729c6a121c66cbf439b06be488ffecb519ccdbd9`. This controlled source
import is **not a clean installed-wheel integration qualification**.

Fake-page regressions were run first, including deliberate red proofs that
unsafe native calls/typed wrapping occurred before correction. Tests never render
hostile geometry. Literal parity covers 76 rectangle/DPI/rotation decisions plus
12 clip/rotation decisions. Eleven candidate factory cases verify propagation of
a smaller worker budget and fatal type. Eleven separate checks against installed
0.1.8 verify release blocking. The new public tests exercise five job profiles
without storage writes and four sync error envelopes. Small native fixtures cover
ordinary OCR text/searchable PDF/structured extraction/Markdown/markup, rotation
and CropBox output geometry, DPI metadata, Word output and editor compilation.

Commands use `/usr/bin/time -v timeout 120s prlimit --as=1073741824 --cpu=90 --`
before `env ... python -m pytest -q --tb=short`. Set
`PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1`.
The interpreter is `pdfnest-worker/.venv/bin/python` in the preserved original
checkout. Use `PYTHONPATH=.` for pinned-package checks; for candidate checks set
`PYTHONPATH=.:<clean-candidate>/src F11_SDK_CANDIDATE_ROOT=<clean-candidate>`.
Run sequentially; do not run the full suites or large cancellation/load fixtures.

Final qualification on this source candidate:

| Run | Result | Wall time (pytest) | Maximum RSS (`time -v`) |
| --- | --- | --- | --- |
| 18 focused files, exact SDK source candidate | 371 passed, 11 skipped | 11.76 s | 396,652 KiB |
| Installed pinned SDK 0.1.8, `test_raster_sdk_boundary.py -k actual_pinned_sdk08` | 11 passed | 0.61 s | 140,244 KiB |
| Small native compatibility selection below (includes one overlapping projection test) | 9 passed | 1.45 s | 209,984 KiB |

The 11 candidate-run skips are exactly the installed-0.1.8 tests exercised in
the separate command. The focused file list is the four new `test_raster_*.py`
files, `test_ocr_v2_worker_core.py`, `test_structured_ocr.py`,
`test_ocr_v2_markup.py`, `test_ocr_text_engine_boundary.py`,
`test_document_extraction_engine_boundary.py`, `test_searchable_pdf_engine_boundary.py`,
`test_ocr_markup_engine_boundary.py`, `test_editor_ocr_engine_boundary.py`,
`test_legacy_editor_ocr_engine.py`, `test_legacy_markup_ocr_engine.py`,
`test_studio_editor_extraction_engine.py`, `test_studio_markup_region_ocr_engine.py`,
`test_pdf_to_markdown_engine_boundary.py`, and `test_pdf_to_word_ocr_engine.py`.

The native selection uses pinned-package imports and runs the whole small
`test_studio_editor_compile_geometry.py` file plus these exact node names:

- `test_raster_geometry_contract.py::test_failed_ocr_page_cannot_become_structured_blank_or_success`
- `test_ocr_cancellation.py::test_extract_text_cancellation_before_start`
- `test_ocr_cancellation.py::test_extract_text_cancellation_during_ocr`
- `test_pdf_to_word_parallelism.py::test_ordinary_page_keeps_existing_raster_default`
- `test_pdf_to_word_parallelism.py::test_scanned_pdf_to_word_uses_structured_ocr_v2`

All final commands returned zero with no measured process swaps. The highest
RSS across all qualification runs, including an earlier run before a new mock
fixture correction, was 400,316 KiB. No memory cap was increased after a failure.
Before native qualification the host had 6,457 MiB available RAM, 12,096 MiB free
swap and 7.7 GiB disk space. No test/Tesseract process was left from prior runs.
The 1 GiB address-space cap is per process, not a production aggregate guarantee.

## Published SDK 0.1.9 integration qualification (M1-S2-47)

`pyproject.toml`, `requirements.txt` and `uv.lock` now agree on SDK 0.1.9.
The targeted `uv lock --upgrade-package platen-document` changed only the root
SDK requirement and SDK package record; the other 82 package records are unchanged.
Runtime dependency floors and Python >=3.12 are unchanged. Production Dockerfiles
still install from the frozen project lock; no Dockerfile or selector default changed.

Fresh PyPI metadata returned HTTP 200 on 2026-10-20. Downloaded distribution bytes
matched both the registry metadata and approved release hashes:

- `platen_document-0.1.9-py3-none-any.whl` (90,554 bytes):
  `70919a01664643b7b5e95d38d893b61b326b9e57bb0a0ddadf79d135c214e21b`.
- `platen_document-0.1.9.tar.gz` (106,437 bytes):
  `74634f3e23dda9ed1740e1a840e263db47ab255a6a431ade8fe7bdff5d2f90f1`.

**Cryptographic verification of PyPI attestation signatures remains incomplete.**
Matching file hashes and provenance claims do not establish signature verification.
No SDK rebuild, publication or production installation was performed here.

The disposable local environment used Python 3.12.13 at
`/home/gimesha/.cache/platen-f11-sdk019-w767q6oa/venv/bin/python`; SDK imports resolved
to its own `lib/python3.12/site-packages/platen_document/__init__.py`. All 37 installed
SDK Python runtime files matched the hash-verified published wheel byte-for-byte.
No candidate SDK source root, editable SDK installation or shared SDK import was used.

Local preparation limitation: registry downloads of large existing dependencies
were too slow for the 120-second setup command. To avoid changing the shared venv,
80 support packages with exactly matching locked versions were copied independently
into the disposable environment; every supplied nonempty RECORD hash was checked
while copying (10,285 files, 491,420,978 bytes). SDK 0.1.8 and console entrypoints
were excluded. The complete `uv sync --frozen --no-install-project` then installed
SDK 0.1.9 from the registry, and `uv pip check` accepted all 81 Linux-installed
packages. This is an isolated installed-SDK qualification with reused local support
dependencies, not a claim of a fresh network download of every dependency. GitHub
CI performs the full clean frozen installation without that local preparation.

The Word regression first failed through `execute_pdf_to_word_ocr` with the
original `JobCancelledException` wrapped as `PdfToWordOcrEngineExecutionError`.
Adding that application cancellation type to the existing fatal passthrough is
the only runtime behavior change in this follow-up. The same test then passed;
ordinary non-cancellation wrapping and raster/configuration propagation remain tested.

| Current installed-package run | Result | Pytest time | Maximum RSS |
| --- | --- | --- | --- |
| Word context-cancellation regression, before fix | 1 failed as expected | 1.48 s | 193,296 KiB |
| Same regression after narrow passthrough | 1 passed | 1.44 s | 190,376 KiB |
| SDK boundary suite before native cases | 190 passed, 5 deselected | 2.94 s | 222,620 KiB |
| Six small native cases, including real scan OCR and expected native-only searchable rejection | 6 passed | 3.74 s | 233,088 KiB |
| Exact CI-designated combined selection | **457 passed, 0 skipped** | **15.22 s** | **423,744 KiB** |

The first boundary-only run preceded addition of the sixth native negative case,
so its console summary was 190 passed / 5 deselected. The final combined selection
contains all 196 SDK-boundary cases. It preserves 76 admission and 12 clip decisions,
11 actual factory constructions, 11 incapable-SDK checks and 11 configuration-error
translations. All 11 real selector paths plus independent markup preview reject
before native/PNG/PIL/probe/adapter work under smaller and mandatory-default budgets.
There are 23 selector cancellation cases: context callbacks for all 12 paths and
explicit callbacks for the 11 paths that support that argument. Word's interface
remains context-only. Nested worker/SDK contexts reset on cancellation. Both Word
and Markdown factories retain per-page DPI and the active smaller budget.

Small output fixtures use one 300x400-point page; actual OCR inputs are 600x800
pixels and render at normal 200 DPI. Text and searchable cases assert OCR recognition;
the other cases verify structured Word/Markdown projection and markup annotations.
The initial vector-only searchable success fixture was rejected by the existing
`SOURCE_IMAGE_MISSING` rule, present in both SDK 0.1.8 and 0.1.9. This is retained
as an explicit negative test proving preexisting destination and staging safety;
the supported searchable fixture includes its source image. Validation was not weakened.

The CI workflow uses Python 3.12, uv 0.11.28, the updated frozen lock, installed
origin/version/hash checks and the exact 23 file/node arguments listed in its
embedded runner. The 18 focused files from the historical selection remain;
the small editor compile file and four approved cancellation/Word nodes follow.
Source-candidate flags and PYTHONPATH are unset. A plugin fails on collection or
execution skips. Controlled execution of that actual runner with one passing
temporary test returned 0, and with one deliberately skipped test returned 1.
Normal PR CI evidence at the resulting HEAD is recorded in the implementation
report; this local result does not assert GitHub CI has already succeeded.

Every pytest command used 1 GiB address space, 90 CPU seconds and 120 seconds
wall time, disabled plugin autoload/bytecode/cache writes, single-thread numeric
libraries and sequential execution. The final combined run used 21.87 user plus
1.05 system CPU seconds and 16.87 wall seconds, with zero measured swaps.
Available RAM was 5,073 MiB before preparation and 8,312 MiB before the combined
native run. An initial uv command hit the 1 GiB address-space cap at 42,428 KiB RSS;
reducing its concurrency to one completed the lock update under the same cap.
No memory cap was increased. Full production container builds and load/stress
suites were not run locally; existing API/actor builds remain mandatory in PR CI.

## Compatibility and remaining gates

A4/Letter at 200 and 300 DPI remain admitted. At 600 DPI those paper sizes exceed
10m and reject even though 600 is a valid setting. Previously accepted large
rasters, extreme axes/coordinates, malformed overrides and noninteger DPI now
reject. Conservative edge guards can reject a page whose nominal area barely fits;
PDF-to-Word's existing adaptive selection may select a slightly lower DPI at that
boundary. Other consumers never silently downsample, skip pages or change engines.

Before merge: independently review the new integration HEAD and its installed
package/CI evidence. The SDK publication gate is complete, but that alone does not
qualify worker integration or deployment. No merge, deployment, selector change
or production capacity change is part of this patch. Main-branch CI already
publishes container images; PR CI retains build-only conditions.

This does **not** close F-11. Pixel admission cannot bound decoded embedded images,
fonts, display lists, transparency, PDF parsing/text extraction, compressed streams,
PNG/PIL copies, retained results or library bugs. One 10m RGB raster is already
about 30 MB before amplification; visual validation retains two native buffers.
Concurrent jobs/legacy page executors multiply those allocations. Existing page,
timeout and concurrency controls are not a total memory budget. Native PyMuPDF
calls still execute in-process and cannot be interrupted by cooperative cancellation
or an asyncio/thread timeout while C code is running. The checks only observe
cancellation before/after return. A separately reviewed killable subprocess boundary
with aggregate/per-process memory and CPU/wall limits is still needed for untrusted
native processing. Existing Tesseract subprocess control and pdf2docx conversion
isolation do not isolate these PyMuPDF calls. Source-image decode budgets and
external package allocations remain separate boundaries. F-12 session lifecycle
and renderer isolation remain distinct work.

## Native OCR operation isolation follow-up

The earlier remaining-gates paragraphs describe the raster-only baseline.
The operation boundary is now `native_operations` → `native_isolation` →
`native_supervisor` → `native_child`, with an explicit allowlist and fresh exec (no forked Python
runtime, pickle, arbitrary module/function from requests, or runtime opt-out).

Production OCR call sites use this boundary for internal and SDK selectors:
V2 text, searchable PDF, document extraction, structured Markdown, markup and
preview, general/legacy/Studio editor extraction and legacy/Studio markup.
Legacy OCR/image-searchable, legacy Markdown and complete PDF-to-Word conversion
also cross it. V2 input inspection, selected-page derivative creation, searchable
image normalization, native editor color recovery, editor compilation and the
actor's retained structured fallback are included, so these native operations do
not precede or follow the child boundary in the long-running API/actor.
Low-level engine classes remain ordinary library APIs for composition **inside**
the child; future application call sites must use `native_operations`.

A child tightens inherited hard limits before importing PDF/OCR implementations:
1 GiB address space, 180 CPU seconds, 64 MiB per output file, 128 descriptors,
no core dumps. The parent enforces a 300-second wall deadline and a 30-second
capacity wait. A private uid-owned flock slot admits one document child across
cooperating API/actor processes sharing that filesystem and temporary root.
Child numeric/OCR page parallelism and Tesseract capacity are set to one.
Limits are mandatory, not permissive environment settings. Inherited stricter
limits remain stricter. Previously allowed longer jobs or larger outputs may
now fail safely instead of occupying an unbounded native operation.

IPC uses bounded JSON with allowlisted dataclass/enum reconstruction: 16 MiB
per frame, 1,000 callbacks / 64 MiB cumulative callback data, 64 KiB each stdout
and stderr. Callbacks execute in the parent; acknowledgement is required before
the child continues, especially `after_ocr` validation before PDF rendering.
The active smaller raster budget crosses exec explicitly. SDK capability checks,
raster ceilings, engine selection and existing output validation remain intact.

Nested Worker Tesseract/pdf2docx helpers inherit the document process group.
Inside the child only, SDK 0.1.9's copied Tesseract runner is bound to the existing
bounded Worker runner, preventing detached/unbounded SDK helper processes.
Timeout/cancellation kills the group and waits for the direct supervisor;
the supervisor normally waits/reaps the native child. Its non-native loop also
kills the group on owner death or its independent wall deadline. Descendant
exit/reaping by the OS is distinct from direct-child wait; no live helper may
continue processing. Child temporaries use a parent-owned private directory;
file outputs and renderer sibling staging stay there. The parent publishes a
successful decoded result's output atomically, preserving an existing destination
on failure and removing partial work with the directory.

Scope limitations remain explicit: RLIMIT_AS/CPU are per process, not aggregate
process-tree or host cgroup budgets. The flock is not cross-node/container
admission. This is resource containment, not a separate uid/seccomp/network/file
access sandbox. GPU/provider allocations and all non-OCR native PDF operations
are not certified by this change. F-12 raster/session safeguards are retained;
preview-session creation metadata parsing remains outside this OCR boundary.
No production runtime, deployment, failover or aggregate-memory qualification is
claimed, and F-11 must not be described as an unconditional whole-Worker sandbox.

An ungraceful owner death can leave bounded private staging files for operational
cleanup; it must not leave live native processing. No cgroup or service-manager
configuration is changed by this patch. Whole-Worker F-11 closure remains partial
until aggregate resource/runtime qualification and non-OCR native paths are covered.
