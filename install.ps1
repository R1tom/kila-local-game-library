# Game Library installer: creates a private Python environment next to this file, installs the
# dependencies, adds Desktop / Start menu / Startup shortcuts and opens the app.
# Run it through install.bat, or:  powershell -ExecutionPolicy Bypass -File install.ps1
param([switch]$NoShortcuts)

$here = $PSScriptRoot
$venvPy = Join-Path $here "venv\Scripts\python.exe"
$pyw = Join-Path $here "venv\Scripts\pythonw.exe"

if (-not (Test-Path $venvPy)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py"; $pyArgs = @("-3") }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python"; $pyArgs = @() }
    else {
        Write-Host "Python 3.10 or newer was not found." -ForegroundColor Red
        Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
        exit 1
    }
    Write-Host "Creating the Python environment..."
    & $py @pyArgs -m venv (Join-Path $here "venv")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Write-Host "Could not create the environment." -ForegroundColor Red; exit 1 }
}

Write-Host "Installing dependencies..."
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "Dependency install failed (see above)." -ForegroundColor Red; exit 1 }

if ($NoShortcuts) { Write-Host "Done (no shortcuts)."; exit 0 }

$ws = New-Object -ComObject WScript.Shell
function New-Shortcut($path, $script) {
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $pyw
    $s.Arguments = "`"$(Join-Path $here $script)`""
    $s.WorkingDirectory = $here
    $s.IconLocation = Join-Path $here "ui\icon.ico"
    $s.Save()
}
New-Shortcut (Join-Path $here "Game Library.lnk") "app.pyw"   # notifications open this one
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Game Library.lnk") "app.pyw"
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "Game Library.lnk") "app.pyw"
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Startup")) "Game Library (background).lnk") "tracker.pyw"

Write-Host "Installed. Opening Game Library..." -ForegroundColor Green
Start-Process $pyw "`"$(Join-Path $here 'app.pyw')`"" -WorkingDirectory $here
