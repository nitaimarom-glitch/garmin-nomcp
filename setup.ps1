# Windows setup, twin of setup.sh.
#
#   .\setup.ps1              full install
#   .\setup.ps1 -Slim        skip the `mcp` package (~30 MB smaller, see below)
#
# If PowerShell refuses to run this ("running scripts is disabled on this
# system"), that is Windows' default execution policy, not a problem with the
# script. Either allow local scripts once:
#     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# or run this file without changing anything:
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# garminconnect is pinned to 0.3.2 deliberately: that is the version the MCP
# server itself resolves to, and several tools (schedule_week, set_heart_rate_zones)
# call client.post / client.request directly, a surface 0.3.x reshuffled.
param([switch]$Slim)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# $ErrorActionPreference only promotes *cmdlet* errors. A native .exe returning
# non-zero sails straight past it, which would let this script announce success
# after pip or the test suite failed. So every native call goes through here.
function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit $LASTEXITCODE). Setup stopped; nothing above is usable."
    }
}

# pip needs git to resolve `garmin-mcp @ git+https://...`, and Windows does not
# ship git. Fail here with a pointer rather than inside a pip traceback.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required but not on PATH. Install it from https://git-scm.com/download/win and re-run."
}

# Find a real Python 3.12+. Testing that `py` merely exists is not enough -- the
# launcher can be present while the requested version is not installed.
$python = $null
foreach ($candidate in @(@("py", "-3.12"), @("py", "-3"), @("python"), @("python3"))) {
    $exe, $flags = $candidate[0], @($candidate[1..($candidate.Count - 1)])
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $version = (& $exe @flags -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
    $major, $minor = $version.Trim() -split "\."
    if ([int]$major -eq 3 -and [int]$minor -ge 12) {
        $python = @{ Exe = $exe; Flags = $flags; Version = $version.Trim() }
        break
    }
}
if (-not $python) {
    throw "No Python 3.12+ found. Install it from https://www.python.org/downloads/ and re-run."
}

Write-Host "Using Python $($python.Version) via '$($python.Exe) $($python.Flags -join ' ')'"
Invoke-Checked "creating the virtualenv" { & $python.Exe @($python.Flags) -m venv .venv }

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "venv created but $venvPy is missing." }

Invoke-Checked "upgrading pip"        { & $venvPy -m pip install --quiet --upgrade pip }
Invoke-Checked "installing garmin-mcp" { & $venvPy -m pip install --quiet "garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp" }
Invoke-Checked "installing garminconnect" { & $venvPy -m pip install --quiet "garminconnect==0.3.2" }

if ($Slim) {
    # `mcp` is imported by garmin_mcp/__init__.py and used by nothing this script
    # calls, so garmin.py stubs it when absent. Dropping it also drops pydantic,
    # starlette, uvicorn and the rest of the server-side tree.
    Write-Host "Slim: removing the unused mcp dependency tree"
    # Not Invoke-Checked: uninstalling a package that was never pulled in is fine.
    & $venvPy -m pip uninstall --quiet --yes mcp pydantic pydantic-core pydantic-settings `
        anyio jsonschema jsonschema-specifications referencing rpds-py attrs click `
        sse-starlette starlette uvicorn httpx httpx-sse httpcore h11 python-multipart 2>$null
    $global:LASTEXITCODE = 0
}

Invoke-Checked "the self-check (test_garmin.py)" { & $venvPy test_garmin.py }

Write-Host ""
Write-Host "Done. Use:  .\garmin.cmd doctor"
Write-Host "First time on this machine? Run:  .\garmin.cmd login"
