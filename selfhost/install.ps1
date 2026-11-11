# Megatokyo ERP - start (installs itself the first time). Run by double-clicking 啟動.bat.
# Also used to update: download the new version, then double-click 啟動.bat again (data is kept).
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Say($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Yellow }
function Done($m) { Write-Host $m -ForegroundColor Green }
function Fail($m) {
    Write-Host ""
    Write-Host $m -ForegroundColor Red
    Read-Host "按 Enter 關閉"
    exit 1
}
function FixedUrl() {
    if (-not (Test-Path ".env")) { return $null }
    $m = Select-String -Path ".env" -Pattern '^ERP_PUBLIC_BASE_URL=(https://\S+)$' | Select-Object -First 1
    if ($m -and $m.Matches[0].Groups[1].Value -notmatch 'localhost') { return $m.Matches[0].Groups[1].Value }
    return $null
}
function RandBytes($n) {
    $b = New-Object byte[] $n
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return $b
}
function RandText($n) {
    $s = ""
    while ($s.Length -lt $n) { $s += ([Convert]::ToBase64String((RandBytes 48)) -replace '[^A-Za-z0-9]', '') }
    return $s.Substring(0, $n)
}

Write-Host "Megatokyo ERP" -ForegroundColor Cyan

# ---------- 1. Docker Desktop ----------
Say "1/5 檢查 Docker Desktop"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $exe = Join-Path $Env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $exe)) {
        Fail "找不到 Docker Desktop。請照「使用說明.txt」第 1 步安裝，裝好後重新開機，再按一次「啟動」。"
    }
    Write-Host "正在開啟 Docker Desktop，第一次可能要 1～3 分鐘…"
    Start-Process $exe
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop 還沒準備好。請等它左下角顯示綠色「Engine running」，再按一次「啟動」。" }
}
Done "Docker Desktop 正常"

# ---------- 2. Secrets (first time only) ----------
$first = -not (Test-Path ".env")
# A database left from an earlier install whose .env (its key) is gone can never be opened again.
if ($first) {
    docker volume inspect megatokyo_pgdata *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "找到之前安裝留下的資料庫，但它的鑰匙檔（.env）不見了，所以打不開。" -ForegroundColor Yellow
        Write-Host "如果您還沒開始正式使用（裡面沒有重要資料），可以清掉重新建立。" -ForegroundColor Yellow
        Write-Host "如果裡面已經有資料：請先不要繼續，把舊資料夾裡的 .env 放回這個 selfhost 資料夾，或找 backups 裡的「金鑰」檔改名為 .env。" -ForegroundColor Yellow
        $ans = Read-Host "要清掉舊資料庫、重新建立嗎？輸入 Y 再按 Enter（其他鍵 = 取消）"
        if ($ans -ne "Y" -and $ans -ne "y") { Fail "已取消，沒有刪除任何東西。" }
        docker compose down -v
    }
}
New-Item -ItemType Directory -Force -Path "data\files", "backups" | Out-Null
if ($first) {
    Say "2/5 第一次使用：產生這台電腦專用的密鑰"
    $lines = @(
        "# Megatokyo ERP secrets for this computer. KEEP A COPY (USB drive). Never share or email this file.",
        "POSTGRES_PASSWORD=$(RandText 40)",
        "ERP_JWT_SECRET=$(RandText 64)",
        "ERP_DATA_ENCRYPTION_KEY=$([Convert]::ToBase64String((RandBytes 32)))",
        "ERP_CRON_SECRET=$(RandText 48)",
        "COMPOSE_PROFILES=cloudflare"
    )
    [System.IO.File]::WriteAllLines((Join-Path $PSScriptRoot ".env"), $lines, (New-Object System.Text.UTF8Encoding($false)))
    # restoring a backup on another computer needs these keys too
    Copy-Item ".env" "backups\金鑰-還原備份時需要-請勿外流.env" -Force
    Done "已產生（存在 .env，並複製一份到 backups 資料夾）"
} else {
    Say "2/5 使用既有的密鑰與資料"
}

# older installs: keep the Cloudflare address running unless a fixed address was set up
if (-not (Select-String -Path ".env" -Pattern '^COMPOSE_PROFILES=' -Quiet)) { Add-Content -Path ".env" -Value "COMPOSE_PROFILES=cloudflare" }

# ---------- 3. Build + database ----------
Say "3/5 準備系統（第一次約 5～15 分鐘，之後很快）"
docker compose build app
if ($LASTEXITCODE -ne 0) { Fail "系統建置失敗。請確認網路正常，再按一次「啟動」；還是不行請把這個視窗截圖給 Claude。" }
docker compose up -d db
docker compose run --rm --no-deps app alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Start-Sleep -Seconds 10
    docker compose run --rm --no-deps app alembic upgrade head
    if ($LASTEXITCODE -ne 0) { Fail "資料庫更新失敗。請把這個視窗截圖給 Claude。" }
}
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "啟動失敗。請把這個視窗截圖給 Claude。" }
Done "系統已啟動"

# ---------- 4. First admin ----------
docker compose run --rm --no-deps app python -m app.cli has-admin *> $null
$needAdmin = ($LASTEXITCODE -eq 3)
if ($needAdmin) {
    Say "4/5 建立您的管理員帳號"
    Write-Host "（密碼至少 12 個字元，不能包含 Email @ 前面的名字。第一次登入時，系統會請您用手機的驗證器 App 掃 QR Code。）"
    $email = Read-Host "您的 Email"
    $name = Read-Host "顯示名稱（例如：老闆）"
    while ($true) {
        $p1 = Read-Host "設定密碼" -AsSecureString
        $p2 = Read-Host "再輸入一次密碼" -AsSecureString
        $a = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1))
        $b = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p2))
        if ($a -ne $b) { Write-Host "兩次密碼不一樣，請重新輸入。" -ForegroundColor Red; continue }
        $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $a | docker compose run --rm -T --no-deps app python -m app.cli create-admin --email $email --name $name --password-stdin --if-missing
        $ok = ($LASTEXITCODE -eq 0)
        $a = $null; $b = $null
        if ($ok) { Done "管理員帳號已建立：$email"; break }
        Write-Host "沒有建立成功（多半是密碼太短或太簡單），請換一個密碼。" -ForegroundColor Red
    }
} else {
    Say "4/5 管理員帳號已經存在"
}

# ---------- 5. Address for colleagues ----------
Say "5/5 取得給同事用的網址"
$url = FixedUrl
$fixed = [bool]$url
for ($i = 0; (-not $url) -and $i -lt 45; $i++) {
    $log = (docker compose logs tunnel 2>&1 | Out-String)
    $m = [regex]::Matches($log, "https://[a-z0-9-]+\.trycloudflare\.com")
    if ($m.Count -gt 0) { $url = $m[$m.Count - 1].Value; break }
    Start-Sleep -Seconds 2
}
$txt = @("這台電腦：http://localhost:8080")
if ($url) {
    $txt += "同事（任何地方的手機或電腦）：$url"
    if ($fixed) { $txt += "這是固定網址，重新開機也不會變。" }
    else { $txt += "注意：這台電腦重新開機或按「停止」之後，同事的網址會換一個新的，請再按「顯示網址」查看。（雙擊「設定固定網址.bat」可以改成固定網址）" }
} else {
    $txt += "同事用的網址還沒產生，請等 1 分鐘後按「顯示網址」。"
}
[System.IO.File]::WriteAllLines((Join-Path $PSScriptRoot "網址.txt"), $txt, (New-Object System.Text.UTF8Encoding($true)))
Write-Host ""
$txt | ForEach-Object { Write-Host $_ -ForegroundColor Cyan }
Write-Host ""
Write-Host "（也存在「網址.txt」。系統會在背景一直執行，關掉這個視窗沒關係；只要電腦開著、Docker Desktop 開著就能用。）"
Start-Process "http://localhost:8080"
Read-Host "按 Enter 關閉這個視窗"
