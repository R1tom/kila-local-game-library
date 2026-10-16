# Removes Local Game Genesys's shortcuts and stops it. Your data (data\ and config.json) is kept;
# delete this folder afterwards to remove everything.
$here = $PSScriptRoot
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($here) -and $_.CommandLine -match '(app|tracker)\.pyw' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
@(
    (Join-Path $here "Local Game Genesys.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Local Game Genesys.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Local Game Genesys.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Local Game Genesys (background).lnk")
) | ForEach-Object { Remove-Item $_ -ErrorAction SilentlyContinue }
Remove-Item "HKCU:\Software\Classes\AppUserModelId\LocalGameGenesys.App" -Recurse -ErrorAction SilentlyContinue
Write-Host "Local Game Genesys removed. You can now delete $here"
