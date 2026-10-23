# D.0 validation infrastructure — NOT CLOSED

This package is test-only. Production imports / default behavior are untouched.
It does **not** currently admit or construct UI pages. Python audit hooks are
not a Qt / curl / native-code sandbox. There is no unsafe override switch.

From the repository, with output outside the repository:

```powershell
python -B tools/ui_validation_d0/runner.py preflight --root D:\project\steam-mod-manager\_tmp\agent_runs\ui-validation\isolated-run
```

Exit `3` means mandatory admission is NOT CLOSED, not successful validation.
`capture` likewise refuses, even if production has exited, while native I/O,
settings aliases and scheduling integration remain unverified. No QWidget,
fake UI, screenshot, or geometry evidence is created by this refusal.

`fixtures --count 20` creates a new synthetic SQLite fixture only when no known
production SMM process is running. It refuses an existing fixture DB. Schema
constants are read with AST; no core/UI/business module is imported or run.
Large row fixtures support `--count 10000`; they are not performance evidence.

Targeted foundation tests (no UI/business imports):

```powershell
$env:SMM_D0_TEST_OUTPUT = 'D:\project\steam-mod-manager\_tmp\tests\ui-validation'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tools/ui_validation_d0/tests -v
```

Tests only create and query **synthetic** SQLite under this external directory.
They do not establish end-to-end product isolation. The eight async plans and
lazy controlled QThread class have not been connected to production receivers.

Before implementing real page integration, review D.0 reports and close
D002–D005. Implement native-path/capability controls, every QSettings alias,
game discovery, all business worker dispatch and shutdown replacements; validate
negative probes before any QApplication/page creation. Do not set production
mutation context or use production DB / settings / Mod / CAS / backup data.
