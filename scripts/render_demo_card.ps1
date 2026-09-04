# Render scripts/demo_card.html to the README hero PNG with headless Chrome.
#
# Chrome rather than Pillow because the card is a layout (grid columns, rounded corners, a
# shadow), and a browser already does layout. `--default-background-color=00000000` keeps the
# page background transparent, so the shadow fades into whichever GitHub theme the reader uses;
# the device scale factor is what makes the text crisp when GitHub scales the image to 850px.
#
# Usage:  .\scripts\render_demo_card.ps1 [-Out docs/figures/demo.png] [-Scale 2]
param(
    [string]$In = "scripts/demo_card.html",
    [string]$Out = "docs/figures/demo.png",
    [double]$Scale = 2
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw "no Chrome or Edge found; install one or render the card by hand" }

$src = [Uri]::new((Resolve-Path $In).Path).AbsoluteUri
$tmp = Join-Path ([IO.Path]::GetTempPath()) "coder-demo-card"
$png = Join-Path $tmp "shot.png"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Force $tmp | Out-Null

# --window-size is in CSS pixels and must cover the card plus its 24px body padding; the capture
# is cropped to the painted area afterwards, so a generous size costs nothing.
& $chrome --headless --disable-gpu --hide-scrollbars `
    --screenshot="$png" --window-size=1000,900 `
    --force-device-scale-factor=$Scale `
    --default-background-color=00000000 `
    --user-data-dir="$tmp/profile" $src 2>$null | Out-Null
if (-not (Test-Path $png)) { throw "chrome produced no screenshot" }

# Trim the fully transparent margin Chrome leaves below the card, so the PNG is the card plus
# just its shadow.
$crop = @'
import sys
from PIL import Image

src, dest = sys.argv[1], sys.argv[2]
im = Image.open(src).convert("RGBA")
im = im.crop(im.getbbox())
im.save(dest)
print(f"{dest}: {im.width}x{im.height}")
'@
$cropFile = Join-Path $tmp "crop.py"
Set-Content -Path $cropFile -Value $crop -Encoding utf8
uv run python $cropFile $png $Out
