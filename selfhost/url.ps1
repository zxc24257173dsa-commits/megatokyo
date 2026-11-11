# Show the current addresses: the fixed one (Tailscale) if set up, otherwise the Cloudflare one (changes after a restart).
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function FixedUrl() {
    if (-not (Test-Path ".env")) { return $null }
    $m = Select-String -Path ".env" -Pattern '^ERP_PUBLIC_BASE_URL=(https://\S+)$' | Select-Object -First 1
    if ($m -and $m.Matches[0].Groups[1].Value -notmatch 'localhost') { return $m.Matches[0].Groups[1].Value }
    return $null
}
# If this folder is not where the system was installed, say where it is (the running containers know).
function RealFolder() {
    $labels = docker inspect megatokyo-app-1 --format "{{json .Config.Labels}}" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $labels) { return $null }
    $dir = ($labels | ConvertFrom-Json).'com.docker.compose.project.working_dir'
    if ($dir -and ((Resolve-Path $dir -ErrorAction SilentlyContinue).Path -ne (Resolve-Path $PSScriptRoot).Path)) { return $dir.Trim() }
    return $null
}
function WrongFolderCheck() {
    if (Test-Path ".env") { return }
    $real = RealFolder
    if ($real) {
        Write-Host ""
        Write-Host "這個資料夾不是系統安裝的位置。系統裝在：" -ForegroundColor Yellow
        Write-Host "  $real" -ForegroundColor Cyan
        Write-Host "已幫您打開那個資料夾。請把這裡的新檔案複製過去（選「取代」），再在那裡操作。" -ForegroundColor Yellow
        Start-Process explorer.exe $real
        Read-Host "按 Enter 關閉"
        exit 1
    }
}
WrongFolderCheck
$url = FixedUrl
$fixed = [bool]$url
if (-not $url) {
    $log = (docker compose logs tunnel 2>&1 | Out-String)
    $m = [regex]::Matches($log, "https://[a-z0-9-]+\.trycloudflare\.com")
    if ($m.Count -gt 0) { $url = $m[$m.Count - 1].Value }
}
Write-Host ""
Write-Host "這台電腦：http://localhost:8080" -ForegroundColor Cyan
if ($url) {
    Write-Host "同事（任何地方）：$url" -ForegroundColor Cyan
    $note = if ($fixed) { "這是固定網址，重新開機也不會變。" } else { "重新開機後這個網址會變；雙擊「設定固定網址.bat」可以改成固定網址。" }
    Write-Host $note
    [System.IO.File]::WriteAllLines((Join-Path $PSScriptRoot "網址.txt"),
        @("這台電腦：http://localhost:8080", "同事（任何地方的手機或電腦）：$url", $note), (New-Object System.Text.UTF8Encoding($true)))
} else {
    Write-Host "同事用的網址還沒產生。請確認已按過「啟動」，等 1 分鐘再試。" -ForegroundColor Yellow
}
Write-Host ""
Read-Host "按 Enter 關閉"
