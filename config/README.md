# Local configuration

Private credentials live here and are **not** committed.

## Mod.io

1. Copy `modio.json.example` → `modio.json`
2. Set `api_key` to your Mod.io API key (read-only GET access is enough)
3. Keep `api_base_url` as `https://api.mod.io/v1` unless you intentionally use another host

`config/modio.json` is gitignored. Never commit a real API key.
