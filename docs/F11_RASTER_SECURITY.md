# F-11 worker raster admission — source qualification

This source patch is **not merge-ready or production-qualified**. The worker
continues to pin `platen-document==0.1.8`; guarded SDK consumers deliberately
report their existing engine-unavailable failure with that package. There is
no fallback to its unguarded renderer. Internal consumers remain usable.

Verified source identities:

- Worker `main`: `9d38852e7ca1e7b657f7f644823d400553886ff0`.
- Reviewed SDK candidate: `729c6a121c66cbf439b06be488ffecb519ccdbd9`,
  [SDK draft PR 2](https://github.com/gimesha-adikari/platen-document/pull/2).
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
| SDK source candidate's canonical/secondary renderers | same contract through `sdk_raster.py`, including smaller active budgets and cancellation translation | all 11 SDK factory families listed below; **release-blocked with installed 0.1.8** |
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

## Qualification and reproduction

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

## Compatibility and remaining gates

A4/Letter at 200 and 300 DPI remain admitted. At 600 DPI those paper sizes exceed
10m and reject even though 600 is a valid setting. Previously accepted large
rasters, extreme axes/coordinates, malformed overrides and noninteger DPI now
reject. Conservative edge guards can reject a page whose nominal area barely fits;
PDF-to-Word's existing adaptive selection may select a slightly lower DPI at that
boundary. Other consumers never silently downsample, skip pages or change engines.

Before merge: independently review this draft; qualify and publish the reviewed
SDK patch through its own authority; update all real worker dependency pins/lock
to the published package in an explicitly authorized follow-up; rerun clean
installed-package integration and required CI. No unavailable PyPI pin, tag,
package publication, merge, deployment or production capacity change is part of
this patch.

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
