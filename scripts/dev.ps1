# Start the StemDeck dev server.
#
# Data stays in .dev-data inside this repo, not in the installed app's folder.
# uv sync reinstalls the CPU torch pinned in uv.lock, so this skips that sync
# and puts the CUDA 12.4 wheels back if they are missing.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$UvBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path (Join-Path $UvBin "uv.exe")) {
    $env:Path = "$UvBin;$env:Path"
}

$env:STEMDECK_DATA_DIR = Join-Path $Root ".dev-data"
$env:STEMDECK_DEMUCS_DEVICE = "cuda"

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv yet. From the repo: uv sync --system-certs"
}

# The CUDA wheel pulls NumPy 2.5, and Numba (beat grid, BPM, key) refuses
# anything newer than 2.4. Put 2.4.4 back whenever torch is repaired.
$Versions = & $Python -c "import torch, numpy; print(torch.__version__); print(numpy.__version__)"
$Torch, $Numpy = $Versions
if ($Torch -notmatch "cu124") {
    Write-Host "Torch is $Torch. Installing the CUDA 12.4 wheels..."
    uv pip install --system-certs --reinstall --index-url https://download.pytorch.org/whl/cu124 torch torchvision torchaudio
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $Numpy = "0"
}
if ($Numpy -notmatch "^2\.4\.") {
    Write-Host "NumPy is $Numpy. Restoring 2.4.4 for Numba..."
    uv pip install --system-certs "numpy==2.4.4"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
