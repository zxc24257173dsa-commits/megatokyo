# Show the current addresses (the colleague address changes after a restart).
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$log = (docker compose logs tunnel 2>&1 | Out-String)
$m = [regex]::Matches($log, "https://[a-z0-9-]+\.trycloudflare\.com")
Write-Host ""
Write-Host "這台電腦：http://localhost:8080" -ForegroundColor Cyan
if ($m.Count -gt 0) {
    $url = $m[$m.Count - 1].Value
    Write-Host "同事（任何地方）：$url" -ForegroundColor Cyan
    [System.IO.File]::WriteAllLines((Join-Path $PSScriptRoot "網址.txt"),
        @("這台電腦：http://localhost:8080", "同事（任何地方的手機或電腦）：$url"), (New-Object System.Text.UTF8Encoding($true)))
} else {
    Write-Host "同事用的網址還沒產生。請確認已按過「啟動」，等 1 分鐘再試。" -ForegroundColor Yellow
}
Write-Host ""
Read-Host "按 Enter 關閉"
