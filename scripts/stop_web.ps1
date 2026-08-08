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

$stopped = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*companion.web.serve*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }

Write-Host "uvicorn プロセスを $stopped 件停止しました"
