# Make a moving package: database + uploaded files + this computer's keys, in selfhost\backups\move-<date>.
# Copy that folder to the new computer's selfhost\backups and run 還原搬家包.bat there.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
function Fail($m) { Write-Host ""; Write-Host $m -ForegroundColor Red; Read-Host "按 Enter 關閉"; exit 1 }

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
if (-not (Test-Path ".env")) { Fail "這台電腦還沒安裝過系統（找不到 .env），沒有東西可以打包。" }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop 沒有開。請先打開 Docker Desktop，等出現「Engine running」再試一次。" }

Write-Host "正在打包資料，請稍候…" -ForegroundColor Yellow
docker compose up -d db backup *> $null
for ($i = 0; $i -lt 30; $i++) {
    docker compose exec -T db pg_isready -U erp -d erp *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 2
}
$d = Get-Date -Format "yyyyMMdd-HHmm"
$name = "move-$d"
New-Item -ItemType Directory -Force -Path "backups\$name" | Out-Null
docker compose exec -T backup sh -c "pg_dump -Fc -h db -U erp erp > /backups/$name/database.dump && tar -czf /backups/$name/files.tar.gz -C /files ."
if ($LASTEXITCODE -ne 0) { Fail "打包失敗。請把這個視窗截圖給 Claude。" }
Copy-Item ".env" "backups\$name\key.env" -Force

Write-Host ""
Write-Host "搬家包做好了：" -ForegroundColor Green
Write-Host "  $PSScriptRoot\backups\$name" -ForegroundColor Cyan
Write-Host ""
Write-Host "接下來："
Write-Host "  1. 把整個「$name」資料夾傳到新電腦（隨身碟或 Google 雲端硬碟；裡面有鑰匙，不要傳到公開的地方）"
Write-Host "  2. 放進新電腦的 selfhost\backups 資料夾"
Write-Host "  3. 在新電腦雙擊「還原搬家包.bat」"
Write-Host "  4. 新電腦確定可以用之後，這台按「停止.bat」，不要兩台同時開"
Write-Host ""
Start-Process explorer.exe "$PSScriptRoot\backups\$name"
Read-Host "按 Enter 關閉"
