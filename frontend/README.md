# Asset library — offline WebEngine read-only preview

`launch_smm_next.cmd --read-only` now opens the complete HTML/CSS/JS library
through Qt WebEngine and the existing Python `LibraryAdapter`. The Widgets
implementation remains available for comparison; `main.py` remains the legacy
default entry. Writable launch is still refused.

The new page includes virtualized cover cards, authoritative local size/type
labels, search/status/type/category filters, favorites and collection browsing,
independent checkboxes, an Inspector shown only on inspection, and full reading,
identity and resource tabs. All selection, navigation and density state is
in memory. There are no write or external-open commands in its bridge.

WebEngine uses an off-record profile. Request interception and CSP refuse
network and local-file resources; covers arrive as scaled image data through
opaque snapshot tokens. Descriptions use the existing sanitizer. Game-load and
detail generations reject stale responses. Cover jobs are limited to four and
queued jobs outside the viewport are discarded. The image cache is bounded.

Production capture: `tools/web_library_validation.py --output <outside-data-dir>`.
Comparison: `tools/web_library_comparison.py --mode widgets|p23 --game 916440
--output <outside-data-dir>`. Both use the shared instance lock, a native read-only
database, the readonly runtime guard and a suspended mutation context. They
never construct the legacy `MainWindow`. Screenshots use a shown Windows window,
not an offscreen Qt platform. Validation output includes source/config/database
hash comparisons and the legacy application's registry settings fingerprint.

Evidence: `D:/smm-product-redesign-web/README.md`. Technical checks do not grant
visual ACCEPT; product visual approval belongs to the user.

## Historical P4.1 Widgets preview

Start from the project directory with `launch_smm_next.cmd --read-only` or
`.venv\Scripts\python.exe -B launch_smm_next.py --read-only`.
This loads real games and assets through the existing DAL, without migrations.
The old `main.py` remains the legacy entry. Close either frontend before starting
the other; both use the same instance lock.

The admitted preview supports browsing, search/filter, grid density, Inspector
and plain-text full detail. All persistence and external-open commands are
blocked at the UI, Adapter and runtime/SQLite boundaries. Writable launch is
still denied. No real QSettings are used, and no business worker is started.

SQLite WAL-mode readonly access may create an empty WAL and a SHM coordination
file or update SHM mtime. This is not a zero-filesystem-side-effect claim. P41
observed unchanged main DB, metadata, manifest and production settings.
See `D:/smm-product-redesign-p41/` for the exact test scope and remaining unknown
P4 historical database writer. Do not remove locks, WAL/SHM, or change admission
constants to bypass a failure.

## Historical P3 composition notes (before P4 integration)

This is product UI code, not a WebView or screenshot renderer. The existing
`main.py` / `ui.MainWindow` path is unchanged. This package does not import `ui`.

## Composition API

After the separate process-exit, stable-data and isolated-runtime gates pass,
an isolated Qt host may construct `WorkbenchWindow(games)` from
`frontend.shell`, supplying explicit `GameContext` values from
`frontend.models`. The host owns QApplication and its lifetime.

There is deliberately no automatic startup, production fallback, QSettings,
database loader, path discovery, worker or business action in this package.
Do not add a production launch switch until business integration gates pass.

`navigate(key)` selects a retained workspace instance. `workspace_changed`
and `game_changed` communicate presentation intent only. Workspace tabs and
density remain in memory; a game switch clears the unbound search input.
`WorkspaceFrame.content_layout` is the mount point for a later admitted page.

Business buttons are disabled. Search/density are presentation controls only,
explicitly labelled as unbound. No Mod grid or Inspector is implemented in P3.

## Design and migration

The P2.3 colors are frozen in `theme.py`. The stylesheet is attached to the new
window only, never QApplication, so the legacy window is not restyled.
`components.py` owns PageHeader and StatusBadge. `shell.py` owns viewport,
navigation, game context and routes. `models.py` holds presentation inputs.

Keep the old front end while admitting one business page at a time. Preserve
identity, mutation-context, canonical-membership and cover-ownership contracts.
Reverting/removing only this new package leaves the current application path
intact; do not reset unrelated working-tree changes.

P3 initially blocked runtime validation while production SMM was running. After
it exited, three stable samples admitted the closed pure-Qt runner in
`tools/ui_validation_d0/shell_preview.py`. Real offscreen QWidget captures cover
1920x1080 and 1520x900. Windows onscreen sizing was constrained by the available
desktop; offscreen results do not validate Windows DPI or window-manager behavior.
Evidence and the frozen design manifest are in `D:/smm-product-redesign-p3/`.
