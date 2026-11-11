# Give the ERP a fixed https address with Tailscale Funnel (free), instead of the Cloudflare quick tunnel whose
# address changes after every restart. Run by double-clicking 設定固定網址.bat. Safe to run again.
#
# Needs: Tailscale for Windows installed and signed in (https://tailscale.com/download/windows).
# Result: https://<this-computer>.<your-tailnet>.ts.net  -> http://localhost:8080 (this computer), kept across reboots.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function Say($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host ""; Write-Host $m -ForegroundColor Red; Read-Host "按 Enter 關閉"; exit 1 }
function SetEnv($key, $value) {
    $path = Join-Path $PSScriptRoot ".env"
    $lines = @(Get-Content -Encoding UTF8 $path | Where-Object { $_ -notmatch "^$key=" })
    $lines += "$key=$value"
    [System.IO.File]::WriteAllLines($path, $lines, (New-Object System.Text.UTF8Encoding($false)))
}

if (-not (Test-Path ".env")) { Fail "這台電腦還沒安裝系統。請先雙擊「啟動.bat」完成安裝，再設定固定網址。" }

Say "1/4 檢查 Tailscale"
$ts = (Get-Command tailscale.exe -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = Join-Path $Env:ProgramFiles "Tailscale\tailscale.exe" }
if (-not (Test-Path $ts)) {
    Start-Process "https://tailscale.com/download/windows"
    Fail "還沒安裝 Tailscale。已幫您打開下載網頁：下載、安裝，用 Google 帳號登入後，再雙擊一次「設定固定網址.bat」。"
}
$st = & $ts status --json 2>$null | ConvertFrom-Json
if (-not $st -or $st.BackendState -ne "Running") {
    Write-Host "Tailscale 還沒登入，正在打開登入網頁…"
    & $ts login
    $st = & $ts status --json 2>$null | ConvertFrom-Json
    if (-not $st -or $st.BackendState -ne "Running") {
        Fail "Tailscale 還沒登入。請點右下角工作列的 Tailscale 圖示 → Log in，用 Google 帳號登入後，再雙擊一次「設定固定網址.bat」。"
    }
}
$name = ($st.Self.DNSName).TrimEnd(".")
if (-not $name) { Fail "讀不到這台電腦在 Tailscale 的名稱。請把這個視窗截圖給 Claude。" }
$url = "https://$name"
Write-Host "Tailscale 正常，這台電腦的固定網址會是：$url" -ForegroundColor Green

Say "2/4 開啟對外網址（Funnel）"
Write-Host "第一次設定時，下面可能會出現一個 https://login.tailscale.com/... 的網址，"
Write-Host "請複製到瀏覽器打開，按「Enable」（允許），回到這裡它就會繼續。" -ForegroundColor Cyan
& $ts funnel --bg 8080
if ($LASTEXITCODE -ne 0) {
    Fail "開啟對外網址沒有成功。如果剛才有出現網址，請先在瀏覽器按「Enable」，再雙擊一次「設定固定網址.bat」；還是不行請截圖給 Claude。"
}

Say "3/4 讓系統使用固定網址"
SetEnv "ERP_PUBLIC_BASE_URL" $url
SetEnv "COMPOSE_PROFILES" ""          # the changing Cloudflare address is no longer needed
docker compose stop tunnel *> $null
docker compose rm -f tunnel *> $null
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "系統重新啟動失敗。請把這個視窗截圖給 Claude。" }

Say "4/4 測試"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 "$url/healthz"
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep -Seconds 3
}
$txt = @("這台電腦：http://localhost:8080", "同事（任何地方的手機或電腦）：$url", "這是固定網址，重新開機也不會變。")
[System.IO.File]::WriteAllLines((Join-Path $PSScriptRoot "網址.txt"), $txt, (New-Object System.Text.UTF8Encoding($true)))
Write-Host ""
if ($ok) { Write-Host "完成！固定網址可以使用了：" -ForegroundColor Green }
else { Write-Host "設定好了，但這台電腦還連不到它（剛開通時可能要等幾分鐘）。請過 5 分鐘用手機試試：" -ForegroundColor Yellow }
Write-Host "  $url" -ForegroundColor Cyan
Write-Host ""
Write-Host "把這個網址傳給同事，請他們存成書籤。以後不用再傳新網址了。"
Write-Host "（也存在「網址.txt」。Tailscale 要保持登入；建議在 Tailscale 設定裡打開「Run unattended」。）"
Read-Host "按 Enter 關閉"
