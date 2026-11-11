# Restore a moving package (made by 做搬家包.bat) on this computer: replaces this computer's data with it.
# Put the move-<date> folder into selfhost\backups first. Works on a computer that never ran 啟動.bat, too.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function Fail($m) { Write-Host ""; Write-Host $m -ForegroundColor Red; Read-Host "按 Enter 關閉"; exit 1 }
function ReadEnv($path) {
    $h = [ordered]@{}
    foreach ($line in (Get-Content -Encoding UTF8 $path)) {
        if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $h[$Matches[1]] = $Matches[2] }
    }
    return $h
}

$pack = Get-ChildItem -Directory -Path "backups" -Filter "move-*" -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
if (-not $pack) { Fail "在 selfhost\backups 裡找不到搬家包（move- 開頭的資料夾）。請先把搬家包資料夾放進來。" }
foreach ($f in "database.dump", "files.tar.gz", "key.env") {
    if (-not (Test-Path (Join-Path $pack.FullName $f))) { Fail "搬家包「$($pack.Name)」不完整，缺少 $f。請重新做一次搬家包。" }
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop 沒有開。請先打開 Docker Desktop，等出現「Engine running」再試一次。" }

Write-Host ""
Write-Host "要還原的搬家包：$($pack.Name)" -ForegroundColor Cyan
Write-Host "這台電腦上目前的系統資料（如果有）會被搬家包的內容取代。" -ForegroundColor Yellow
$ans = Read-Host "確定要還原嗎？輸入 Y 再按 Enter（其他鍵 = 取消）"
if ($ans -ne "Y" -and $ans -ne "y") { Fail "已取消，沒有改動任何東西。" }

# Keys: the package's keys (they unlock the encrypted data inside), but if this computer already has a database,
# keep the password of that database so it can still be opened.
$old = ReadEnv (Join-Path $pack.FullName "key.env")
docker volume inspect megatokyo_pgdata *> $null
$volume = ($LASTEXITCODE -eq 0)
if ($volume -and (Test-Path ".env")) {
    $cur = ReadEnv ".env"
    if ($cur.Contains("POSTGRES_PASSWORD")) { $old["POSTGRES_PASSWORD"] = $cur["POSTGRES_PASSWORD"] }
}
$lines = @("# Megatokyo ERP secrets for this computer (restored from $($pack.Name)). Keep a copy; never share.")
foreach ($k in $old.Keys) { $lines += "$k=$($old[$k])" }
[System.IO.File]::WriteAllLines((Join-Path $PSScriptRoot ".env"), $lines, (New-Object System.Text.UTF8Encoding($false)))
New-Item -ItemType Directory -Force -Path "data\files" | Out-Null
Copy-Item ".env" "backups\金鑰-還原備份時需要-請勿外流.env" -Force

Write-Host ""
Write-Host "== 準備系統（第一次在這台電腦約 5～15 分鐘）" -ForegroundColor Yellow
docker compose build app
if ($LASTEXITCODE -ne 0) { Fail "系統建置失敗。請確認網路正常再試；還是不行請截圖給 Claude。" }
docker compose up -d db backup
for ($i = 0; $i -lt 60; $i++) {
    docker compose exec -T db pg_isready -U erp -d postgres *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 2
}
docker compose stop app scheduler tunnel *> $null

Write-Host "== 還原資料庫" -ForegroundColor Yellow
docker compose exec -T db psql -U erp -d postgres -c "DROP DATABASE IF EXISTS erp WITH (FORCE)"
docker compose exec -T db psql -U erp -d postgres -c "CREATE DATABASE erp OWNER erp"
docker compose exec -T backup sh -c "pg_restore --no-owner --role=erp -h db -U erp -d erp /backups/$($pack.Name)/database.dump"
if ($LASTEXITCODE -ne 0) { Fail "資料庫還原失敗。請把這個視窗截圖給 Claude（搬家包本身沒有被改動）。" }

Write-Host "== 還原照片與收據" -ForegroundColor Yellow
docker compose run --rm --no-deps --user root -v "${PSScriptRoot}\backups:/restore:ro" app sh -c "tar --no-same-owner -xzf /restore/$($pack.Name)/files.tar.gz -C /data/files && chown -R 10001:10001 /data/files"
if ($LASTEXITCODE -ne 0) { Fail "照片與收據還原失敗。請把這個視窗截圖給 Claude。" }

Write-Host "== 更新資料庫到最新版本並啟動" -ForegroundColor Yellow
docker compose run --rm --no-deps app alembic upgrade head
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "啟動失敗。請把這個視窗截圖給 Claude。" }

Write-Host ""
Write-Host "還原完成！資料、帳號、照片都搬過來了。" -ForegroundColor Green
Write-Host "1 分鐘後雙擊「顯示網址.bat」取得同事用的新網址。"
Write-Host "舊電腦請按「停止.bat」，不要兩台同時開。"
Start-Process "http://localhost:8080"
Read-Host "按 Enter 關閉"
