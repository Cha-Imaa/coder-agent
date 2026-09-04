# Record one full `coder run` on a suite task, and promote the take you like into the README hero.
#
# The run costs about 12k Groq tokens; check the daily quota before starting (docs/results.md).
# That price is why the cast is version controlled: `docs/figures/demo.cast` is 10kB of text and
# it is the only thing standing between a restyle and another paid-for run. Rendering never
# costs anything, so the loop is record once, render as often as the design changes.
#
#   .\scripts\record_demo.ps1                 # take 1, preview GIF, no repository file touched
#   .\scripts\record_demo.ps1 -Take 2         # another take; compare the previews, pick one
#   .\scripts\record_demo.ps1 -Promote 2      # that take becomes the cast and the hero
#
# The task repository is copied to `~/.coder-demo` first, so the recording never touches the
# suite and the path stays short enough to read at 88 columns. The child runs in a 28-row
# terminal but the GIF shows the last 24: the window is recorded once and framed afterwards, so
# the hero can be made shorter without a second run. Every approval prompt is answered "y".
#
# `-Model k2think:MBZUAI-IFM/K2-Think-v2` records a take without touching the Groq daily quota,
# at the price of a longer run: K2 takes about 15 tool-calling steps to gpt-oss-120b's 11.
param(
    [string]$Task = "fix-bug-duration-units",
    [string]$Model = "",
    [int]$Take = 1,
    [int]$Promote = 0,
    [string]$Cast = "docs/figures/demo.cast",
    [string]$Out = "docs/figures/demo.gif",
    [int]$Cols = 88,
    [int]$Rows = 28,
    [int]$ShowRows = 24
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$takes = Join-Path $HOME ".coder-demo"
$takeCast = Join-Path $takes "$Task-take$Take.cast"

if ($Promote -gt 0) {
    $chosen = Join-Path $takes "$Task-take$Promote.cast"
    if (-not (Test-Path $chosen)) { throw "no take $Promote at $chosen" }
    Copy-Item $chosen $Cast -Force
    uv run python scripts/demo.py render $Cast $Out --rows $ShowRows
    Write-Host "hero: $Out (from take $Promote)"
    return
}

$work = Join-Path $takes $Task
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force $takes | Out-Null

$prompt = (uv run python scripts/demo.py prepare $Task $work | Out-String).Trim()
$shown = "coder run ./$Task `"$prompt`""

# A failed run still leaves the cast on disk for inspection; the GIF is only drawn from a
# passing one.
$run = @("coder", "run", $work, $prompt)
if ($Model) { $run += @("--model", $Model) }   # the header prints it, so the hero names its model
uv run python scripts/demo.py record $takeCast --title $shown --cols $Cols --rows $Rows -- @run
if ($LASTEXITCODE -ne 0) { throw "coder run exited with $LASTEXITCODE; cast kept at $takeCast" }

$preview = "docs/figures/preview/demo-take$Take.gif"
New-Item -ItemType Directory -Force (Split-Path $preview) | Out-Null
uv run python scripts/demo.py render $takeCast $preview --rows $ShowRows
Write-Host "take $Take -> $takeCast"
Write-Host "preview: $preview   promote with: .\scripts\record_demo.ps1 -Promote $Take"
