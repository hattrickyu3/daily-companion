# Webアプリを再起動する。コードを変更したあとに使う。
#
# 管理者PowerShellで実行してください。
# S4Uタスクとして起動したプロセスはセッション0で動くため、
# 通常の権限では停止できず「Access is denied」になります。

$ErrorActionPreference = 'Continue'
$taskName = 'daily-companion-web'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Warning '管理者権限が必要です。管理者PowerShellで実行し直してください。'
    exit 1
}

& (Join-Path $PSScriptRoot 'stop_web.ps1')

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Warning "タスクが登録されていません: $taskName"
    Write-Warning 'scripts\register_tasks.ps1 を先に実行してください。'
    exit 1
}

Start-ScheduledTask -TaskName $taskName
Write-Host "タスクを起動しました: $taskName"

# 監視スクリプト経由なので、立ち上がるまで少し待つ。
$port = 8765
$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*WEB_PORT\s*=\s*(\d+)' }
    if ($line) { $port = [int]$Matches[1] }
}

foreach ($attempt in 1..20) {
    Start-Sleep -Seconds 2
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$port/" -UseBasicParsing -TimeoutSec 5
        Write-Host "起動を確認しました: HTTP $($response.StatusCode)  http://localhost:$port"
        exit 0
    } catch {
        continue
    }
}

Write-Warning "40秒待ちましたが応答がありません。logs\web.log を確認してください。"
exit 1
