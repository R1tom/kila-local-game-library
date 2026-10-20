# Removes KILA Local Game Library's shortcuts and stops it. Your data (data\ and config.json) is kept;
# delete this folder afterwards to remove everything.
$name = "KILA Local Game Library"
$here = $PSScriptRoot
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($here) -and $_.CommandLine -match '(app|tracker|game_overlay)\.pyw' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$startup = [Environment]::GetFolderPath("Startup")
@(
    (Join-Path $here "$name.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "$name.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "$name.lnk"),
    (Join-Path $startup "$name (background).lnk"),
    (Join-Path $startup "$name (game overlay).lnk")
) | ForEach-Object { Remove-Item $_ -ErrorAction SilentlyContinue }
Remove-Item "HKCU:\Software\Classes\AppUserModelId\KilaLocalGameLibrary.App" -Recurse -ErrorAction SilentlyContinue
Write-Host "$name removed. You can now delete $here"
Write-Host "Per-game AMD settings you changed from the overlay stay in AMD Adrenalin; reset them there if you want."
