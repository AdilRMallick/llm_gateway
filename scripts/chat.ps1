<#
.SYNOPSIS
  Send a request to the gateway from PowerShell without fighting quoting.

.DESCRIPTION
  `curl` in PowerShell is an alias for Invoke-WebRequest, and passing a JSON body
  through to curl.exe mangles the quotes. This wrapper builds the body as a
  PowerShell object and posts it with Invoke-RestMethod.

.EXAMPLE
  .\scripts\chat.ps1 "why is the sky blue"
  .\scripts\chat.ps1 "hello" -Policy pinned -Provider google
  .\scripts\chat.ps1 "sample me" -Temperature 0.9 -Raw
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Prompt,

    [ValidateSet("cheapest", "fastest", "pinned")]
    [string]$Policy = "cheapest",

    [ValidateSet("anthropic", "openai", "google")]
    [string]$Provider,

    [double]$Temperature = 0.0,
    [int]$MaxTokens = 256,
    [string]$GatewayUrl = "http://localhost:8000",

    # Print the whole JSON response instead of the summary.
    [switch]$Raw
)

$payload = @{
    messages    = @(@{ role = "user"; content = $Prompt })
    policy      = $Policy
    temperature = $Temperature
    max_tokens  = $MaxTokens
}
if ($Provider) { $payload.provider = $Provider }

$json = $payload | ConvertTo-Json -Depth 5

# Windows PowerShell 5.1 decodes response bodies as the ANSI codepage regardless of
# the charset header, which turns any non-ASCII character in a completion into
# mojibake. Read the raw bytes and decode them as UTF-8 ourselves.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $resp = Invoke-WebRequest -Uri "$GatewayUrl/v1/chat" -Method Post `
        -ContentType "application/json" -Body $json -UseBasicParsing
    $r = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray()) | ConvertFrom-Json
}
catch {
    Write-Host "request failed: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    exit 1
}

if ($Raw) {
    $r | ConvertTo-Json -Depth 8
    return
}

$chain = ($r.attempts | ForEach-Object { "$($_.provider):$($_.status)" }) -join " -> "
if (-not $chain) { $chain = "(served from cache)" }

Write-Host ""
Write-Host $r.content
Write-Host ""
Write-Host ("-" * 60) -ForegroundColor DarkGray
Write-Host ("provider  {0} / {1}" -f $r.provider, $r.model)
Write-Host ("cache_hit {0}    latency {1} ms" -f $r.cache_hit, $r.latency_ms)
Write-Host ("tokens    {0} in / {1} out    cost `${2}" -f $r.usage.tokens_in, $r.usage.tokens_out, $r.usage.cost_usd)
Write-Host ("attempts  {0}" -f $chain)
