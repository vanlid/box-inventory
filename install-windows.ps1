# Installs Box Inventory as a background task on Windows (starts when you log in, restarts if it crashes).
# Usage (PowerShell, in this folder):
#   .\install-windows.ps1              install / reinstall
#   .\install-windows.ps1 -Uninstall   stop and remove the task (keeps your data)
#   .\install-windows.ps1 -Port 9000   use another port
# If Windows blocks the script:  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
param([switch]$Uninstall, [int]$Port = 8765)
$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$TaskName = "Box Inventory"
$RuleName = "Box Inventory"
$Me = "$env:USERDOMAIN\$env:USERNAME"
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Uninstall) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  if ($IsAdmin) { Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue }
  Write-Host "Removed the task. Your boxes are still in $Dir\data"
  exit 0
}

# --- Python ---
$Py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
  $Py = (& py -3 -c "import sys; print(sys.executable)" 2>$null)
}
if (-not $Py) {
  $c = Get-Command python -ErrorAction SilentlyContinue
  if ($c -and $c.Source -notlike "*WindowsApps*") { $Py = (& $c.Source -c "import sys; print(sys.executable)") }
}
if (-not $Py) {
  Write-Host "Python 3 not found. Install it with:  winget install Python.Python.3.12   (then open a new PowerShell)"
  exit 1
}
$PyW = Join-Path (Split-Path $Py) "pythonw.exe"   # runs without a console window
if (-not (Test-Path $PyW)) { $PyW = $Py }

# --- Claude Code ---
$Claude = $null
$c = Get-Command claude.exe -ErrorAction SilentlyContinue
if ($c) { $Claude = $c.Source }
elseif (Test-Path "$env:USERPROFILE\.local\bin\claude.exe") { $Claude = "$env:USERPROFILE\.local\bin\claude.exe" }
if (-not $Claude) {
  Write-Host "Claude Code not found. Install it with:  irm https://claude.ai/install.ps1 | iex"
  exit 1
}

# --- Token: the task doesn't see your shell's variables, so keep an existing env token in .claude-token ---
$TokenFile = Join-Path $Dir ".claude-token"
if (-not (Test-Path $TokenFile) -and $env:CLAUDE_CODE_OAUTH_TOKEN) {
  Set-Content -Path $TokenFile -Value $env:CLAUDE_CODE_OAUTH_TOKEN.Trim() -NoNewline -Encoding ascii
  Write-Host "Using CLAUDE_CODE_OAUTH_TOKEN from your shell (saved to .claude-token)."
}
if (Test-Path $TokenFile) {
  icacls $TokenFile /inheritance:r /grant:r "${Me}:F" "SYSTEM:F" | Out-Null   # readable only by you
  $env:CLAUDE_CODE_OAUTH_TOKEN = (Get-Content $TokenFile -Raw).Trim()
  Write-Host "Checking the saved Claude token..."
  $Fix = "The token in .claude-token didn't work. Delete it and set a valid CLAUDE_CODE_OAUTH_TOKEN, or run 'claude setup-token'."
} else {
  Write-Host "No .claude-token found; checking this computer's Claude login..."
  $Fix = "Claude isn't set up. Set CLAUDE_CODE_OAUTH_TOKEN and run this again, or run 'claude' once and log in."
}
foreach ($v in "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
$ErrorActionPreference = "Continue"
& $Claude -p "Reply with just: ok" --no-session-persistence *> $null
$ok = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $ok) { Write-Host $Fix; exit 1 }

# --- Scheduled task ---
New-Item -ItemType Directory -Force (Join-Path $Dir "data") | Out-Null
$Log = Join-Path $Dir "data\server.log"
$action = New-ScheduledTaskAction -Execute $PyW -WorkingDirectory $Dir `
  -Argument "`"$Dir\server.py`" --port $Port --log `"$Log`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Me
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $Me -LogonType Interactive -RunLevel Limited
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description "Box Inventory web server ($Dir)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# --- Firewall: let phones on your home network reach the port ---
if ($IsAdmin) {
  Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
  New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
  $fw = "Firewall: port $Port opened for Private networks."
} else {
  $fw = "Firewall: to reach it from your phone, run this once in an *administrator* PowerShell:`n" +
        "   New-NetFirewallRule -DisplayName '$RuleName' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private"
}

Start-Sleep -Seconds 3
try { Invoke-WebRequest "http://localhost:$Port/api/boxes" -UseBasicParsing -TimeoutSec 5 | Out-Null }
catch { Write-Host "The server didn't start. See the log: $Log"; exit 1 }

Write-Host ""
Write-Host "Box Inventory is running. On your phone (same Wi-Fi) open one of:"
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.InterfaceAlias -notmatch 'vEthernet|WSL|Docker|Loopback|VirtualBox|VMware' } |
  ForEach-Object { Write-Host "   http://$($_.IPAddress):$Port" }
Write-Host $fw
Write-Host "Your Wi-Fi must be set to 'Private network' in Windows settings. Log: $Log"
$code = Join-Path $Dir "data\setup-code.txt"
if (Test-Path $code) { Write-Host ""; Write-Host ("Setup code for your first passkey: " + (Get-Content $code -Raw).Trim()) }
Write-Host ""
Write-Host "Passkey sign-in needs HTTPS. With Tailscale installed: tailscale serve --bg $Port"
Write-Host "then open the https://<name>.ts.net address it shows."
