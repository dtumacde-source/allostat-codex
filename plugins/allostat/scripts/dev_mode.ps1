# dev_mode.ps1 - point the wrapper at the local MCP server for active dev.
#
# Usage:
#   .\scripts\dev_mode.ps1                                 # local default http://127.0.0.1:8765/mcp
#   .\scripts\dev_mode.ps1 -Url http://127.0.0.1:9000/mcp  # custom URL (e.g., different port)
#   .\scripts\dev_mode.ps1 -Url https://mcp-staging.allostat.ai/mcp  # point at staging
#
# Sets ALLOSTAT_MCP_URL in the current PowerShell session. Claude Code
# launched from this shell will use it. Restart Claude Code to pick up
# the change.
#
# Optional: also sets ALLOSTAT_MCP_TOKEN if -Bearer provided. Otherwise
# the wrapper falls back to its bundled .env bearer (which is the prod
# bearer for a normal install — likely 401 against local). Pass -Bearer
# when pointing at local/staging since their bearer namespaces differ
# from prod.
#
# To turn off: run .\scripts\dev_mode_off.ps1 or close the shell.
[CmdletBinding()]
param(
    [string]$Url = 'http://127.0.0.1:8765/mcp',
    [string]$Bearer = $null
)

$env:ALLOSTAT_MCP_URL = $Url

if ($Bearer) {
    $env:ALLOSTAT_MCP_TOKEN = $Bearer
    Write-Host "Set ALLOSTAT_MCP_URL = $Url" -ForegroundColor Green
    Write-Host "Set ALLOSTAT_MCP_TOKEN = <bearer captured>" -ForegroundColor Green
} else {
    Write-Host "Set ALLOSTAT_MCP_URL = $Url" -ForegroundColor Green
    Write-Host "ALLOSTAT_MCP_TOKEN unchanged. If you are pointing at local or staging," -ForegroundColor Yellow
    Write-Host "the prod bearer will likely 401 — pass -Bearer <dev-token> or set" -ForegroundColor Yellow
    Write-Host "ALLOSTAT_MCP_TOKEN manually before launching Claude Code." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Restart Claude Code for the change to take effect." -ForegroundColor Cyan
Write-Host "To return to prod, run: .\scripts\dev_mode_off.ps1" -ForegroundColor DarkGray
