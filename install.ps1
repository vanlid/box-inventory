# Box Inventory for Windows: download (or update) and install, no git needed.
#   irm https://github.com/vanlid/box-inventory/releases/latest/download/install.ps1 | iex
# Run it again to update. Your data\, .env and .claude-token are never touched.
# Options (environment variables): BOX_DIR, BOX_VERSION (latest or v1.2.0), BOX_NO_SERVICE=1 (download only)
$ErrorActionPreference = "Stop"
$Repo = "vanlid/box-inventory"
$Dir = if ($env:BOX_DIR) { $env:BOX_DIR } else { Join-Path $env:USERPROFILE "box-inventory" }
$Version = if ($env:BOX_VERSION) { $env:BOX_VERSION } else { "latest" }
if ($env:BOX_URL) { $Url = $env:BOX_URL }
elseif ($Version -eq "latest") { $Url = "https://github.com/$Repo/releases/latest/download/box-inventory.zip" }
else { $Url = "https://github.com/$Repo/releases/download/$Version/box-inventory.zip" }

$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("box-inventory-" + [guid]::NewGuid())
New-Item -ItemType Directory $Tmp | Out-Null
try {
  Write-Host "Downloading Box Inventory ($Version)..."
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  try { Invoke-WebRequest $Url -OutFile (Join-Path $Tmp "app.zip") -UseBasicParsing }
  catch { throw "Download failed: $Url. If the repository is private, download it with: gh release download -R $Repo -p box-inventory.zip" }
  Expand-Archive (Join-Path $Tmp "app.zip") -DestinationPath $Tmp
  $Src = Join-Path $Tmp "box-inventory"
  if (-not (Test-Path (Join-Path $Src "server.py"))) { throw "That download doesn't look like Box Inventory." }
  New-Item -ItemType Directory -Force $Dir | Out-Null
  Get-ChildItem -Force $Src | Where-Object { $_.Name -notin @("data", ".env", ".claude-token") } |
    Copy-Item -Destination $Dir -Recurse -Force
} finally {
  Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
$EnvFile = Join-Path $Dir ".env"
if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Dir ".env.example") $EnvFile }
$Ver = if (Test-Path (Join-Path $Dir "VERSION")) { (Get-Content (Join-Path $Dir "VERSION") -Raw).Trim() } else { $Version }
Write-Host "Box Inventory $Ver is in $Dir"
if (-not $env:BOX_NO_SERVICE) {
  # A separate process, so this works even where running downloaded scripts is restricted.
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir "install-windows.ps1")
}
