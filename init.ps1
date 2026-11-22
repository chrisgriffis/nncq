<#
.SYNOPSIS
  Set up no-net-code-query on this machine. Run once after cloning:  .\init.ps1

  Everything stays inside this repo folder (git-ignored):
    .venv\              Python environment
    cache\models\hf\    Nemotron weights (pinned)     cache\tools\   pip cache, parsers
    cache\db\           indexes                       cache\logs\    logs
    .env                your settings (written by the interactive step)

  Afterwards:  .venv\Scripts\nncq index      then add the MCP config it prints (.venv\Scripts\nncq mcp-config).

.PARAMETER Lite        Lite mode: no torch, no models, no GPU (~150 MB instead of ~14 GB). Keyword search, the
                      symbol graph and the data cross-reference only. Re-run without -Lite later to upgrade in place.
.PARAMETER Cpu         Install CPU-only torch (no NVIDIA GPU; indexing will be very slow).
.PARAMETER Agents      Set up these agents without asking: copilot,claude,vs (MCP server + nncq-user skill).
.PARAMETER Python      Base interpreter for the venv (3.11-3.13). Default: py -3.12, else python on PATH.
.PARAMETER SeedModels  A Hugging Face hub cache dir that already holds the Nemotron models: copy, don't download.
.PARAMETER Yes         Accept defaults for every prompt (non-interactive).
#>
param(
  [switch]$Lite,
  [switch]$Cpu,
  [string]$Agents = "",
  [string]$Python = "",
  [string]$SeedModels = "",
  [switch]$Yes
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$env:PIP_CACHE_DIR = Join-Path $Root "cache\tools\pip"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PIP_PREFER_BINARY = "1"
$env:HF_HOME = Join-Path $Root "cache\models\hf"
# keep build/temp files inside the repo too
$Tmp = Join-Path $Root "cache\tmp"
New-Item -ItemType Directory -Force $Tmp | Out-Null
$env:TMP = $Tmp; $env:TEMP = $Tmp

if (-not $Python) {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in "3.12", "3.13", "3.11") {
      $Python = (& py "-$v" -c "import sys;print(sys.executable)" 2>$null)
      if ($Python) { break }
    }
  }
  if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }
}
$ver = & $Python -c "import sys;print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.11" -or [version]$ver -ge [version]"3.14") { throw "Python 3.11-3.13 required (got $ver at $Python). Pass -Python <path>." }
Write-Host "== base python $ver : $Python"

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
  Write-Host "== creating .venv"
  & $Python -m venv (Join-Path $Root ".venv"); if ($LASTEXITCODE) { throw "venv creation failed" }
}

& $Py -m pip install --upgrade pip --quiet
if ($Lite) {
  Write-Host "== lite mode: skipping torch and models"
  $Extras = ".[dev]"
} else {
  Write-Host "== installing torch ($(if ($Cpu) {'cpu'} else {'cuda 12.8'}))"
  $TorchIndex = if ($Cpu) { "https://download.pytorch.org/whl/cpu" } else { "https://download.pytorch.org/whl/cu128" }
  & $Py -m pip install "torch==2.11.0" --index-url $TorchIndex --quiet; if ($LASTEXITCODE) { throw "torch install failed" }
  $Extras = ".[gpu,dev]"
}

Write-Host "== installing nncq"
& $Py -m pip install -e $Extras --quiet; if ($LASTEXITCODE) { throw "nncq install failed" }

$initArgs = @("-m", "nncq", "init")
if ($Lite) { $initArgs += "--lite" }
if ($Agents) { $initArgs += @("--agents", $Agents) }
if ($SeedModels) { $initArgs += @("--seed-models", $SeedModels) }
if ($Yes) { $initArgs += "--yes" }
& $Py @initArgs
exit $LASTEXITCODE
