# Register the OpenStack MCP in Claude Code on Windows.
#
# This is an HTTP MCP — the client needs only Claude Code + network access to the
# server. No Python or dependencies are installed; this just registers the
# endpoint and your per-user credentials.
#
# Run in PowerShell:
#   .\install-client.ps1
# (If blocked by execution policy:  powershell -ExecutionPolicy Bypass -File .\install-client.ps1)
$ErrorActionPreference = "Stop"

function PromptDefault($label, $default) {
    $v = Read-Host "$label [$default]"
    if ([string]::IsNullOrWhiteSpace($v)) { return $default } else { return $v }
}
function PromptSecret($label) {
    $s = Read-Host $label -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function EnvOr($name, $fallback) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($v)) { return $fallback } else { return $v }
}
# Like EnvOr but the fallback is a scriptblock invoked ONLY when the env var is empty.
# PowerShell evaluates call arguments eagerly, so `EnvOr "X" (Read-Host ...)` would
# prompt even when $env:X is set; passing a {scriptblock} makes the prompt truly lazy
# (matches the .sh `${X:-$(prompt)}` behavior).
function AskOr($name, [scriptblock]$prompt) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($v)) { return (& $prompt) } else { return $v }
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Error "'claude' CLI not found. Install Claude Code first."
    exit 1
}

$Prefix = EnvOr "MCP_NAME" "openstack"     # entries named <Prefix>-<domain>
$Scope  = EnvOr "SCOPE" "user"             # local | user | project ; user = everywhere
$DefaultDomains = "compute network storage lbaas image identity observability"
$Domains = (EnvOr "DOMAINS" $DefaultDomains) -split '\s+' | Where-Object { $_ }

# Server base (scheme://host:port, NO path), and the OpenStack cloud it talks to for YOU.
$Base   = (AskOr "BASE_URL" { PromptDefault "MCP server base URL (no path)" "http://192.168.140.14:8001" }).TrimEnd('/')
$OsAuth = AskOr "OS_AUTH_URL" { PromptDefault "OpenStack (Keystone) auth URL" "http://192.168.140.14:5000/v3" }

# Your credentials for that cloud.
$OsId     = AskOr "OS_APP_CRED_ID"     { Read-Host "OpenStack app-credential id" }
$OsSecret = AskOr "OS_APP_CRED_SECRET" { PromptSecret "OpenStack app-credential secret" }

# Best-effort `claude mcp remove`: removing a non-existent entry makes claude.exe
# write to stderr, which the npm `claude.ps1` wrapper re-raises as a terminating
# NativeCommandError under our `$ErrorActionPreference = "Stop"`. A `2>$null` redirect
# only silences the stream — it does NOT catch the exception — so wrap in try/catch
# and reset the exit code.
function Remove-McpQuiet($name) {
    try { claude mcp remove --scope $Scope $name 2>$null | Out-Null } catch { }
    $global:LASTEXITCODE = 0
}

function Add-Entry($name, $url) {
    Remove-McpQuiet $name
    claude mcp add --transport http --scope $Scope $name $url `
        -H "X-OS-Auth-Url: $OsAuth" `
        -H "X-OS-App-Cred-Id: $OsId" `
        -H "X-OS-App-Cred-Secret: $OsSecret"
    Write-Host "Registered  $name  -> $url"
}

Write-Host ""
$RegNames = @()
foreach ($d in $Domains) {
    if ($d -eq "all") { Add-Entry $Prefix "$Base/mcp"; $RegNames += $Prefix }
    else { Add-Entry "$Prefix-$d" "$Base/$d/mcp"; $RegNames += "$Prefix-$d" }
}
Write-Host ""
Write-Host "Done (scope: $Scope). Verify with:  claude mcp list"
Write-Host ""
Write-Host "NOTE: Claude Code may prompt for permission on first use of each tool."
Write-Host "      If you want to pre-approve these servers, add to your settings.json"
Write-Host "      under permissions.allow, one entry per registered server:"
foreach ($n in $RegNames) { Write-Host "        mcp__${n}__*" }

# Install bundled client-side skills, if this package includes any.
$ScriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ScriptDir)) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not [string]::IsNullOrWhiteSpace($ScriptDir)) {
    $SkillsSrc = Join-Path (Split-Path -Parent $ScriptDir) "skills"
    if ((Test-Path $SkillsSrc) -and (Get-ChildItem -Path $SkillsSrc -Directory -ErrorAction SilentlyContinue)) {
        $Dest = Join-Path $HOME ".claude/skills"
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
        foreach ($d in Get-ChildItem -Path $SkillsSrc -Directory) {
            Copy-Item -Recurse -Force -Path $d.FullName -Destination $Dest
            Write-Host "Installed skill: $($d.Name) -> $Dest"
        }
    }
}

# Auto-save this connection as an /openstack-profile profile (reuses bundled openstack-profile.ps1).
$ProfilePs1 = Join-Path (Split-Path -Parent $ScriptDir) "skills/openstack-profile/openstack-profile.ps1"
if (Test-Path $ProfilePs1) {
    $ProfileName = if ($env:PROFILE) { $env:PROFILE } else { PromptDefault "Save this connection as profile (blank to skip)" "default" }
    if ($ProfileName) {
        # openstack-profile.ps1 reads creds from $env:*, so we must set them — but we run it
        # in-process, so naively setting $env:OS_APP_CRED_SECRET etc. would LEAK into the shell
        # session and make a re-run of this installer silently skip the secret prompt
        # (AskOr sees the leftover value). Save the prior values, set, then restore in
        # finally so the session is left exactly as we found it.
        $creds = @{
            OPENSTACK_PROFILE_NONINTERACTIVE="1"
            BASE_URL=$Base
            OS_APP_CRED_ID=$OsId; OS_APP_CRED_SECRET=$OsSecret; OS_AUTH_URL=$OsAuth
        }
        $prevEnv = @{}
        foreach ($k in $creds.Keys) {
            $prevEnv[$k] = [Environment]::GetEnvironmentVariable($k)
            Set-Item -Path "Env:$k" -Value $creds[$k]
        }
        # Invoke in-process with `&` — we are already inside PowerShell. Do NOT shell
        # out to `pwsh`: that is PowerShell 7's exe, absent on stock Windows (which
        # ships only Windows PowerShell 5.1 as `powershell.exe`), so `pwsh ...` throws
        # CommandNotFoundException and the profile silently never saves.
        try {
            & $ProfilePs1 add $ProfileName
            Write-Host "Saved profile '$ProfileName' — switch later with /openstack-profile."
        } catch {
            Write-Host "(profile auto-save skipped: $($_.Exception.Message))"
        } finally {
            foreach ($k in $creds.Keys) {
                if ($null -eq $prevEnv[$k]) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
                else { Set-Item -Path "Env:$k" -Value $prevEnv[$k] }
            }
        }
    }
}
