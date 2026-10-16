# Removes Game Library's shortcuts and stops it. Your data (data\ and config.json) is kept;
# delete this folder afterwards to remove everything.
$here = $PSScriptRoot
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($here) -and $_.CommandLine -match '(app|tracker)\.pyw' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
@(
    (Join-Path $here "Game Library.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Game Library.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Game Library.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Game Library (background).lnk")
) | ForEach-Object { Remove-Item $_ -ErrorAction SilentlyContinue }
Remove-Item "HKCU:\Software\Classes\AppUserModelId\GameLibrary.App" -Recurse -ErrorAction SilentlyContinue
Write-Host "Game Library removed. You can now delete $here"
