# 常駐しているWebアプリを止める。
#
# 監視スクリプト(run_web.ps1)がいる状態でuvicornだけを落とすと
# 10秒後に起動し直されてしまうので、タスクごと止めてからプロセスを掃除する。

$ErrorActionPreference = 'Continue'
$taskName = 'daily-companion-web'

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $taskName
    Write-Host "タスクを停止しました: $taskName"
} else {
    Write-Host "タスクが見つかりません（手動起動のみの状態です）: $taskName"
}

$targets = New-Object System.Collections.Generic.HashSet[int]

# コマンドラインで見つける。通常はこれで足りる。
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*companion.web.serve*' } |
    ForEach-Object { [void]$targets.Add([int]$_.ProcessId) }

# コマンドラインが読めないプロセスもあり、それだけだと取り逃がして
# ポートを掴んだまま居座る。待ち受けポートの持ち主からも辿る。
$port = 8765
$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*WEB_PORT\s*=\s*(\d+)' }
    if ($line) { $port = [int]$Matches[1] }
}

Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { [void]$targets.Add([int]$_.OwningProcess) }

foreach ($id in $targets) {
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}

# 止めたつもりで止まっていないのが一番困るので、必ず結果を確かめる。
Start-Sleep -Seconds 2
$remaining = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)

if ($remaining.Count -eq 0) {
    Write-Host "停止しました（ポート $port は解放されています）"
    exit 0
}

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Warning "ポート $port をまだ PID $($remaining[0].OwningProcess) が使用しています。"
if ($isAdmin) {
    Write-Warning '管理者で実行していますが停止できませんでした。タスクスケジューラから手動で終了してください。'
} else {
    # S4Uタスクはセッション0で動くため、通常の権限では終了できない。
    Write-Warning 'S4Uタスクとして起動したプロセスは、管理者権限がないと停止できません。'
    Write-Warning '管理者PowerShellで、このスクリプトをもう一度実行してください。'
}
exit 1
