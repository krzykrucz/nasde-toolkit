# Check that $env:CURSOR_API_KEY is set for nasde's Cursor agent.
#
# Windows-equivalent of scripts/export_cursor_api_key.sh. Harbor's Cursor agent
# authenticates only via CURSOR_API_KEY. There is no known local store to read
# the key from on Windows, so this script verifies the variable and explains
# how to set it.
#
#     . .\scripts\export_cursor_api_key.ps1
#     nasde run --variant cursor-vanilla -C my-benchmark

if (-not [string]::IsNullOrEmpty($env:CURSOR_API_KEY)) {
    $preview = $env:CURSOR_API_KEY.Substring(0, [Math]::Min(8, $env:CURSOR_API_KEY.Length))
    Write-Host "OK CURSOR_API_KEY already set ($preview...)" -ForegroundColor Green
    return
}

Write-Error "CURSOR_API_KEY is not set. Create a key in the Cursor dashboard (Integrations -> API keys), then run: `$env:CURSOR_API_KEY = '<your-key>' -- or add CURSOR_API_KEY=<your-key> to .env in the benchmark project dir (or its parent); nasde run reads it itself."
