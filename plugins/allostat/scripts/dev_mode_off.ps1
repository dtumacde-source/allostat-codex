# dev_mode_off.ps1 - return the wrapper to prod by clearing the override env-var.
#
# Usage:
#   .\scripts\dev_mode_off.ps1
#
# Clears ALLOSTAT_MCP_URL in the current PowerShell session. After this,
# the wrapper falls back to its DEFAULT_ENDPOINT (https://mcp.allostat.ai/mcp).
# Restart Claude Code to pick up the change.
#
# Does NOT clear ALLOSTAT_MCP_TOKEN — if you set a dev bearer with dev_mode.ps1
# -Bearer, you may want to clear it too. The wrapper will then read its
# bundled .env bearer (the prod bearer) on next launch.
[CmdletBinding()]
param(
    [switch]$ClearBearer
)

Remove-Item Env:ALLOSTAT_MCP_URL -ErrorAction SilentlyContinue
Write-Host "Cleared ALLOSTAT_MCP_URL. Wrapper will use prod default." -ForegroundColor Green

if ($ClearBearer) {
    Remove-Item Env:ALLOSTAT_MCP_TOKEN -ErrorAction SilentlyContinue
    Write-Host "Cleared ALLOSTAT_MCP_TOKEN. Wrapper will read bundled .env bearer." -ForegroundColor Green
}

Write-Host ""
Write-Host "Restart Claude Code for the change to take effect." -ForegroundColor Cyan
