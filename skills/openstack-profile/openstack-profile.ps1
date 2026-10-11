# /openstack-profile backend (Windows). Mirrors openstack-profile.sh. Profiles in
# $env:OPENSTACK_PROFILE_DIR or %APPDATA%\openstack-mcp\profiles. claude via
# $env:CLAUDE_BIN (default 'claude') for test stubbing.
param([string]$Cmd = "list", [string]$Name = "")
$ErrorActionPreference = "Stop"

$ProfileDir = if ($env:OPENSTACK_PROFILE_DIR) { $env:OPENSTACK_PROFILE_DIR } else { Join-Path $env:APPDATA "openstack-mcp\profiles" }
$ClaudeBin  = if ($env:CLAUDE_BIN) { $env:CLAUDE_BIN } else { "claude" }
$Scope      = if ($env:SCOPE) { $env:SCOPE } else { "user" }
$Prefix     = if ($env:MCP_NAME) { $env:MCP_NAME } else { "openstack" }
$DefaultDomains = "compute","network","storage","lbaas","image","identity","observability"

# Name validation: reject path traversal and non-alphanumeric names (except _ and -)
# Applied to add/switch/remove, not list (empty name is OK for list)
if ($Name -and $Name -notmatch '^[A-Za-z0-9_-]+$') {
  Write-Error "invalid profile name: '$Name' (use letters/digits/_/- only)"
  exit 1
}

function Need($v, $n) { if (-not $v) { Write-Host "$n required" -ForegroundColor Red; exit 1 } ; $v }

# UTF-8 without BOM, version-agnostic. `Set-Content -Encoding utf8NoBOM` is PS7+ only —
# Windows PowerShell 5.1 rejects that encoding value, breaking add/switch. .NET works everywhere.
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
function Write-Utf8Lines($path, $lines) { [System.IO.File]::WriteAllLines($path, [string[]]$lines, $Utf8NoBom) }
function Write-Utf8Text($path, $text)   { [System.IO.File]::WriteAllText($path, [string]$text, $Utf8NoBom) }

# Best-effort native `claude mcp remove`: removing a non-existent entry makes claude
# write to stderr, which the npm `claude.ps1` wrapper re-raises as a terminating error
# under `$ErrorActionPreference = "Stop"`. `2>$null` silences the stream but not the
# exception — so wrap in try/catch and reset the exit code.
function Remove-McpQuiet($n) {
  try { & $ClaudeBin mcp remove --scope $Scope $n 2>$null | Out-Null } catch { }
  $global:LASTEXITCODE = 0
}

# Interactive prompt helpers (used by `add` when stdin is a TTY).
function Read-Secret($label) {
  $sec = Read-Host $label -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

switch ($Cmd) {
  "list" {
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    $af = Join-Path $ProfileDir ".active"
    $active = if (Test-Path $af) { (Get-Content $af -Raw).Trim() } else { "" }
    Write-Host "profiles ($ProfileDir):"
    $items = Get-ChildItem -Path $ProfileDir -Filter *.env -ErrorAction SilentlyContinue
    if ($items) {
      $items | ForEach-Object {
        if ($_.BaseName -eq $active) { Write-Host "  * $($_.BaseName)  (active)" } else { Write-Host "  - $($_.BaseName)" }
      }
    } else { Write-Host "  (none)" }
  }
  "add" {
    if (-not $Name) { Write-Host "usage: openstack-profile.ps1 add <name>"; exit 1 }
    if ($env:FROM) {
      $ff = Join-Path $ProfileDir "$($env:FROM).env"
      if (-not (Test-Path $ff)) { Write-Host "no such source profile: $($env:FROM)" -ForegroundColor Red; exit 1 }
      Get-Content $ff | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') {
          $k = $matches[1]; $v = $matches[2].Trim("'")
          if (-not [System.Environment]::GetEnvironmentVariable($k)) { Set-Item -Path "Env:$k" -Value $v }
        }
      }
    }
    # Hybrid input: prompt for any missing field when interactive. A value already
    # in the environment (incl. inherited via FROM) is kept and never prompted, so
    # non-interactive callers (automation/tests passing env) fall through to the
    # Need checks below instead of hanging on a prompt.
    # DOMAINS is intentionally NOT prompted (advanced; defaults to all). Set it via
    # env only — `switch` still honors a DOMAINS line if a profile has one.
    if ((-not [Console]::IsInputRedirected) -and (-not $env:OPENSTACK_PROFILE_NONINTERACTIVE)) {
      while (-not $env:BASE_URL)          { $env:BASE_URL = Read-Host "BASE_URL (e.g. http://192.168.140.14:8001 — scheme required, no path)" }
      while (-not $env:OS_AUTH_URL)       { $env:OS_AUTH_URL = Read-Host "OS_AUTH_URL (e.g. http://192.168.140.14:5000/v3)" }
      while (-not $env:OS_APP_CRED_ID)    { $env:OS_APP_CRED_ID = Read-Host "OS_APP_CRED_ID" }
      while (-not $env:OS_APP_CRED_SECRET) { $env:OS_APP_CRED_SECRET = Read-Secret "OS_APP_CRED_SECRET" }
    }
    $base = Need $env:BASE_URL "BASE_URL"
    $authUrl = Need $env:OS_AUTH_URL "OS_AUTH_URL"
    $id = Need $env:OS_APP_CRED_ID "OS_APP_CRED_ID"; $secret = Need $env:OS_APP_CRED_SECRET "OS_APP_CRED_SECRET"
    # A schemeless BASE_URL produces a broken MCP url at switch time. Assume http://
    # when no scheme was given, and strip any trailing slash.
    if ($base -notmatch '^https?://') { Write-Host "note: BASE_URL has no scheme — assuming http://$base"; $base = "http://$base" }
    $base = $base.TrimEnd('/')
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    $f = Join-Path $ProfileDir "$Name.env"
    $lines = @(
      "BASE_URL=$base", "OS_AUTH_URL=$authUrl", "OS_APP_CRED_ID=$id", "OS_APP_CRED_SECRET=$secret"
    )
    if ($env:DOMAINS) { $lines += "DOMAINS='$env:DOMAINS'" }
    Write-Utf8Lines $f $lines
    Write-Host "wrote profile '$Name' -> $f"
  }
  "switch" {
    if (-not $Name) { Write-Host "usage: openstack-profile.ps1 switch <name>"; exit 1 }
    $f = Join-Path $ProfileDir "$Name.env"
    if (-not (Test-Path $f)) { Write-Host "no such profile: $Name" -ForegroundColor Red; exit 1 }
    $p = @{}
    Get-Content $f | ForEach-Object {
      if ($_ -match '^([^=]+)=(.*)$') {
        $k = $matches[1]; $v = $matches[2]
        if ($k -eq 'DOMAINS') { $v = $v.Trim("'") }
        $p[$k] = $v
      }
    }
    $domains = if ($p.DOMAINS) { $p.DOMAINS -split '\s+' } else { $DefaultDomains }
    function AddEntry($n, $url) {
      Remove-McpQuiet $n
      & $ClaudeBin mcp add --transport http --scope $Scope $n $url `
        -H "X-OS-Auth-Url: $($p.OS_AUTH_URL)" `
        -H "X-OS-App-Cred-Id: $($p.OS_APP_CRED_ID)" `
        -H "X-OS-App-Cred-Secret: $($p.OS_APP_CRED_SECRET)"
      Write-Host "registered $n -> $url"
    }
    foreach ($d in $domains) { AddEntry "$Prefix-$d" "$($p.BASE_URL)/$d/mcp" }
    Write-Utf8Text (Join-Path $ProfileDir ".active") $Name
    Write-Host "switched to '$Name' ($($p.BASE_URL))."
    Write-Host "apply with:  claude --continue  (resume latest conversation)  or  claude --resume  (pick a session) — keeps your conversation, reloads MCP."
  }
  "remove" {
    if (-not $Name) { Write-Host "usage: openstack-profile.ps1 remove <name>"; exit 1 }
    $f = Join-Path $ProfileDir "$Name.env"
    if (Test-Path $f) {
      Remove-Item $f
      $af = Join-Path $ProfileDir ".active"
      if ((Test-Path $af) -and ((Get-Content $af -Raw).Trim() -eq $Name)) { Remove-Item $af }
      Write-Host "removed profile '$Name'"
    } else { Write-Host "no such profile: $Name" }
  }
  default { Write-Host "usage: openstack-profile.ps1 [list | add <name> | switch <name> | remove <name>]"; exit 1 }
}
