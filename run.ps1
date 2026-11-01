<#
.SYNOPSIS
  Runs PDF Extractor from source (Mode 1). Creates a venv and installs dependencies on first run.
.EXAMPLE
  .\run.ps1                                   # opens the GUI
  .\run.ps1 -Url https://example.com/report   # CLI mode
  .\run.ps1 -Url https://example.com -Deep -Headed -Open
  .\run.ps1 -Web                              # web UI at http://localhost:8000
.NOTES
  If script execution is blocked:  Set-ExecutionPolicy -Scope Process Bypass
#>
param(
    [string]$Url,
    [string]$Out = "$HOME\Downloads\PDF_Extractor",
    [switch]$Headed,
    [switch]$Deep,
    [switch]$Open,
    [switch]$NoPrompt,          # skip save prompts: save into -Out using each PDF's suggested name
    [int]$Wait = 6,
    [string]$Password,
    [switch]$Web,               # serve the browser UI instead of the desktop GUI
    [int]$Port = 8000,
    [switch]$InstallChromium   # only needed if the PC has neither Edge nor Chrome
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
}
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
$req = if ($Web) { "requirements-web.txt" } else { "requirements.txt" }
& $py -m pip install --quiet -r $req
if ($InstallChromium) { & $py -m playwright install chromium }

if ($Web) {
    Write-Host "Web UI: http://localhost:$Port" -ForegroundColor Cyan
    & $py -m uvicorn server:app --host 127.0.0.1 --port $Port
    return
}

$argv = @("pdf_extractor.py")
if ($Url)      { $argv += $Url; $argv += @("--out", $Out, "--wait", $Wait) }
if ($Headed)   { $argv += "--headed" }
if ($Deep)     { $argv += "--deep" }
if ($Open)     { $argv += "--open" }
if ($NoPrompt) { $argv += "--no-prompt" }
if ($Password) { $argv += @("--password", $Password) }
& $py @argv
