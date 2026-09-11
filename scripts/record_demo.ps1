# Record one full `coder run` on a suite task and render it as the README hero GIF.
#
# The task repository is copied to a temporary directory first, so the recording never touches
# the suite. The run costs about 12k Groq tokens for the default task; check the daily quota
# before starting (see docs/results.md). Every approval prompt is answered "y" after a short pause.
#
# Usage:  .\scripts\record_demo.ps1 [-Task fix-bug-duration-units] [-Out docs/figures/demo.gif]
param(
    [string]$Task = "fix-bug-duration-units",
    [string]$Out = "docs/figures/demo.gif",
    [int]$Rows = 28
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$work = Join-Path ([IO.Path]::GetTempPath()) "coder-demo/$Task"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force (Split-Path $work) | Out-Null

$prompt = (uv run python scripts/demo.py prepare $Task $work | Out-String).Trim()
$cast = "$work.cast"
$shown = "coder run ./$Task `"$prompt`""

# A failed run still leaves the cast on disk for inspection; the GIF is only drawn from a
# passing one.
uv run python scripts/demo.py record $cast --title $shown --rows $Rows -- coder run $work $prompt
if ($LASTEXITCODE -ne 0) { throw "coder run exited with $LASTEXITCODE; cast kept at $cast" }
uv run python scripts/demo.py render $cast $Out --rows $Rows
Write-Host "cast: $cast"
