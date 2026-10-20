# KILA Local Game Library installer: checks the PC (AMD Radeon + Adrenalin, WebView2, Python), creates a
# private Python environment next to this file, installs the dependencies, adds Desktop / Start menu /
# Startup shortcuts and opens the app.
# Run it through install.bat, or:  powershell -ExecutionPolicy Bypass -File install.ps1
param([switch]$NoShortcuts)

$name = "KILA Local Game Library"
$here = $PSScriptRoot
$venvPy = Join-Path $here "venv\Scripts\python.exe"
$pyw = Join-Path $here "venv\Scripts\pythonw.exe"

# --- PC check: this app is made for AMD Radeon graphics only
$gpus = @(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name)
if (-not ($gpus -match 'AMD|Radeon')) {
    Write-Host "$name is for PCs with an AMD Radeon graphics card only." -ForegroundColor Red
    Write-Host "  Found: $($gpus -join ', ')"
    Write-Host "  The in-game overlay, FPS, GPU stats and per-game AMD settings all come from the AMD driver."
    exit 1
}
Write-Host "Graphics: $(($gpus -match 'AMD|Radeon') -join ', ')" -ForegroundColor Green
if (-not (Test-Path "$env:SystemRoot\System32\amdadlx64.dll")) {
    Write-Host "The AMD Software: Adrenalin Edition driver was not found (amdadlx64.dll is missing)." -ForegroundColor Red
    Write-Host "  Install the latest Adrenalin driver from https://www.amd.com/en/support/download/drivers.html, then run this again."
    exit 1
}
Write-Host "AMD Adrenalin driver: found" -ForegroundColor Green
$wv = Get-ItemProperty "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                       "HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" -ErrorAction SilentlyContinue |
      Where-Object { $_.pv -and $_.pv -ne "0.0.0.0" } | Select-Object -First 1
if (-not $wv) {
    Write-Host "Microsoft Edge WebView2 Runtime is missing (the app window needs it)." -ForegroundColor Red
    Write-Host "  Install the 'Evergreen Bootstrapper' from https://developer.microsoft.com/microsoft-edge/webview2/, then run this again."
    exit 1
}
Write-Host "WebView2: $($wv.pv)" -ForegroundColor Green

# --- Python environment
if (-not (Test-Path $venvPy)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py"; $pyArgs = @("-3") }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python"; $pyArgs = @() }
    else {
        Write-Host "Python 3.10 or newer was not found." -ForegroundColor Red
        Write-Host "Install it from https://www.python.org/downloads/ (64-bit, tick 'Add python.exe to PATH'), then run this again."
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
$startup = [Environment]::GetFolderPath("Startup")
New-Shortcut (Join-Path $here "$name.lnk") "app.pyw"   # notifications open this one
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "$name.lnk") "app.pyw"
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "$name.lnk") "app.pyw"
New-Shortcut (Join-Path $startup "$name (background).lnk") "tracker.pyw"
New-Shortcut (Join-Path $startup "$name (game overlay).lnk") "game_overlay.pyw"

Write-Host ""
Write-Host "Installed. Before playing, check the 'Set up Windows, Steam and AMD' part of README.md:" -ForegroundColor Green
Write-Host "  - turn off Xbox Game Bar's controller button (Settings > Gaming > Game Bar)"
Write-Host "  - turn off 'Guide Button Focuses Steam' (Steam > Settings > Controller)"
Write-Host "  - use borderless / windowed fullscreen in games"
Write-Host "Opening $name..."
Start-Process $pyw "`"$(Join-Path $here 'app.pyw')`"" -WorkingDirectory $here
Start-Process $pyw "`"$(Join-Path $here 'game_overlay.pyw')`"" -WorkingDirectory $here
