# Phase 5L read-only candidate semantics — results

The new inspector is an isolated prototype operation. No actor, API route, compiler or production preview invokes it. It is registered in the existing native boundary so its source-file authority, resource admission and cleanup receive the same controller tests as other operations. It accepts only candidate bytes and an expected artifact SHA-256; there is no editor string or operation payload input.

## Actual results

- `tests/test_candidate_selection.py`: 25 passed. Includes eight Sinhala/Tamil sequences, exact transparent patch matching, two clean white searchable-scan words, complete RGB patch equivalence, native visible Base14/WinAnsi ASCII words, artifact hash mismatch, arbitrary hidden text, moved/reused/cross-page/altered/obscured images, annotation and visible text conflicts, clipping, crop/rotation and unsupported font controls.
- Focused compiler/protocol/script/refusal group: 83 passed, including sheared image refusal and deliberately misaligned hidden OCR refusal; no output artifact and source hash unchanged.
- Controller profile/word protocol/initial selection group: 111 passed before later test additions; the final full suite contains the final tests.
- Final qualified full suite: **1,611 passed, 10 skipped, 2 warnings in 224.07 seconds**, no failures.
- Twelve new two-page candidates: qpdf passed, exact MuPDF and Poppler target text, one target search hit, old target absent, source image streams retained, page geometry preserved. **144** renderer/DPI/page comparisons gave zero unauthorized off-mask changes; control pages pixel-identical.

The earlier known resource test failure was reproduced under the inherited development persistent-pool configuration, then passed with `ENABLE_PERSISTENT_RENDER_POOL=false`. No assertion or production resource limit was altered. Tests independently asserting rejection of unsafe managed persistent-pool configuration remain in the passing full suite. Ten forensic fixture skips are not passes.

## Reproducible environment

Use the worker virtual environment and `F11_INSTALLED_ENV_ROOT` pointing at that environment. Set `APP_ENV=test`, `ENABLE_PERSISTENT_RENDER_POOL=false`, `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `PYTHONDONTWRITEBYTECODE=1`, an isolated render-session root, and `REDIS_URL=redis://127.0.0.1:6381/14` for the existing local stack's test database.

The markdown-admission fixture needs an executable `redis-server`. A task-owned PATH wrapper launched actual Redis 7.4.9 from `redis:7-alpine`, forwarding all fixture arguments, using host networking and mounting only the fixture's temporary directory. The fixture selected a random loopback port, disabled persistence, limited its own instance to 32 MiB, and terminated its own child after use. No shared database was flushed and no fixture assertions were skipped.

```sh
ENABLE_PERSISTENT_RENDER_POOL=false F11_INSTALLED_ENV_ROOT="$PWD/.venv" \
PYTHONPATH=. PATH=/tmp/platen-phase5l-test-bin:$PATH APP_ENV=test \
REDIS_URL=redis://127.0.0.1:6381/14 \
PDFNEST_RENDER_SESSION_DIR=/tmp/platen-phase5l-worker-closure \
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python -m pytest -q --tb=short
```

Native operation and semantic-layer production integration remain unqualified until real browser selection/copy, whole application lifecycle, authorization and accessibility gates pass. No new application workflow or UI-download parity is claimed here. Sinhala/Tamil human review remains pending.
