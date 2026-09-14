# Local configuration

Private credentials live here and are **not** committed.

## Mod.io

1. Copy `modio.json.example` → `modio.json`
2. Set `api_key` to your Mod.io API key (read-only GET access is enough)
3. Keep `api_base_url` as `https://api.mod.io/v1` unless you intentionally use another host

`config/modio.json` is gitignored. Never commit a real API key.

## Startup Identity Reconcile

`config/startup_runtime.json` controls whether GUI launch walks the whole Mod
library for Identity Reconcile:

- Default: `startup_reconcile_enabled=false` (DB projection only — no folder walk)
- Override: env `SMM_STARTUP_RECONCILE_ENABLED=1`
- When enabled: delay + batch pause + optional `max_mods` cap

Repair / tools can still call `reconcile_library` / `start_reconcile_library_async`
for a full unpaced pass.

## Load order

Per-game SMM load-order files live in `config/load_order/`:

- Stellaris: `config/load_order/stellaris.json`
- Total War: WARHAMMER III: `config/load_order/wh3.json`

These JSON files are gitignored. They store SMM sort tokens only — never Paradox / WH3 launcher identifiers.
