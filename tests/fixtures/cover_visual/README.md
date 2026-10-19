# Cover presentation regression inputs

These nine small JPEG files are frozen **real production QWebChannel cover
responses**, not synthetic covers. `manifest.json` records their input SHA-256,
the original file SHA-256/dimensions, game/source identity, and review focus.
The original production images are never rewritten. Tests use an isolated
read-only fixture database and substitute only these recorded image responses;
the formal probe uses the actual production resolver and current original files.

The set covers landscape, square, portrait, center text, edge text/subjects,
five source platforms, and a real missing-cover case. The caption sample has
an explicit required source region. Other subjects are not automatically
guaranteed to survive intentional `cover` cropping: those are visual-review cases.

Run the automated contract:

```
python -m pytest tests/test_web_cover_visual.py tests/test_card_scheme_c.py tests/test_web_library.py
```

The tests verify image/container alignment, actual visible source bounds,
resource identity with Inspector, painted Qt pixels, warm-cache scroll return,
and an intentionally reintroduced original defect. Pixel comparison masks only
the cover edges and existing overlaid controls. Raw pixel errors and heatmaps
are retained; the acceptance metric averages signed color differences within
8×8 physical-pixel blocks (maximum mean error 6/255) to tolerate Canvas/CSS
downsampling and subpixel rasterization differences. Geometry and the required
caption region are checked independently. Captures require two animation frames
and consecutive stable native frames, not merely `img.complete`.

Formal production evidence requires closing any existing SMM window normally:

```
python tools/cover_visual_probe.py --output D:/smm-cover-review/run-125-standard --viewport 1728x900 --expected-dpr 1.25
python tools/cover_visual_probe.py --output D:/smm-cover-review/run-125-small --viewport 1180x760 --expected-dpr 1.25
```

On the audited Windows desktop, `QT_SCALE_FACTOR=0.8` gives actual DPR 1.0
because the desktop DPR is 1.25. Use a separate process and record the measured
DPR; do not assume this multiplier is correct on another desktop. Run Qt probes
and Qt tests serially, since they share foreground/compositor state.

The probe writes `results.json`, actual crops, centered-cover mathematical
references, difference heatmaps, full Qt screenshots, and screenshot hashes.
Its output directory must be outside the production project. Exit codes:

- `0`: automatic checks and every required user-approved reference passed.
- `1`: mismatch, capture error, or startup failure.
- `2`: automatic checks passed, but user visual approval is still pending.

## User-approved references

There is currently **no user-approved corrected baseline**. The referenced
historical user screenshots depict the defect and are not good-image references.
Do not relabel generated candidates as approved to make a check pass.

After explicit user review, preserve the accepted crop PNGs under `approved/`
and add entries to `user_visual_review.approved_references`, each containing
`key`, logical `viewport`, actual `dpr`, relative `file`, file `sha256`, and
`approval_evidence` identifying the user's decision. Keep the corresponding
full-window screenshot and DOM report in the evidence archive. Reference
changes require another explicit user decision; no auto-update/force switch is
provided. Missing approval stays `USER VISUAL REVIEW PENDING`.
