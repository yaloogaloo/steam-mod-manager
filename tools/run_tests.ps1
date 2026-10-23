# Runs only the requested tests; child-process environment changes are restored.
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$temporary = Join-Path $repository "_tmp\tests\python-$PID"
$base = Join-Path $repository "_tmp\tests\pytest-$PID"
$oldValues = @{}
foreach ($name in @('TEMP', 'TMP', 'TMPDIR', 'PYTHONDONTWRITEBYTECODE')) {
    $oldValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    foreach ($candidate in @($temporary, $base)) {
        $ancestor = [IO.Path]::GetFullPath($candidate)
        while ($ancestor) {
            if ((Test-Path -LiteralPath $ancestor) -and
                ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Temporary path contains a reparse point: $ancestor"
            }
            $ancestor = [IO.Path]::GetDirectoryName($ancestor)
        }
    }
    New-Item -ItemType Directory -Force -Path $temporary | Out-Null
    foreach ($name in @('TEMP', 'TMP', 'TMPDIR')) {
        [Environment]::SetEnvironmentVariable($name, $temporary, 'Process')
    }
    $env:PYTHONDONTWRITEBYTECODE = '1'
    Push-Location $repository
    try {
        & python -B -m pytest --basetemp $base @args
        $testExitCode = $LASTEXITCODE
    } finally { Pop-Location }
} finally {
    foreach ($name in $oldValues.Keys) {
        [Environment]::SetEnvironmentVariable($name, $oldValues[$name], 'Process')
    }
}
exit $testExitCode
