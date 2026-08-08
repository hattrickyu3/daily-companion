# daily-companion の定期ジョブをWindowsタスクスケジューラに登録する。
#
# 管理者として実行してください（S4Uログオンの登録に管理者権限が必要なため）。
#   右クリック → 「PowerShell で実行」ではなく、
#   管理者PowerShellで scripts\register_tasks.ps1 を実行する。
#
# S4Uログオンを使うので、Windowsにログインしていなくてもジョブが動く。
# パスワードの保存も不要。既存の discord-claude-bot と同じ方式。

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"

$jobs = @(
    @{ Name = 'daily-companion-morning'; Script = 'morning_brief.bat';  Time = '07:30'; Desc = '朝のブリーフィング' }
    @{ Name = 'daily-companion-evening'; Script = 'evening_prompt.bat'; Time = '22:00'; Desc = '夜の日記催促' }
    @{ Name = 'daily-companion-weekly';  Script = 'weekly_review.bat';  Time = '21:00'; Desc = '週次の振り返り'; Weekly = 'Sunday' }
)

foreach ($job in $jobs) {
    $batPath = Join-Path $root "scripts\$($job.Script)"
    if (-not (Test-Path $batPath)) {
        throw "スクリプトが見つかりません: $batPath"
    }

    $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$batPath`"" -WorkingDirectory $root

    if ($job.Weekly) {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $job.Weekly -At $job.Time
    } else {
        $trigger = New-ScheduledTaskTrigger -Daily -At $job.Time
    }

    # S4U: パスワードなしで、ログインしていなくても実行する。
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

    # StartWhenAvailable: PCが落ちていて実行時刻を逃した場合、起動後に追いかけて実行する。
    # 電源断が起きうる環境なので、取りこぼしを拾えるようにしておく。
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $job.Desc -Force | Out-Null

    $schedule = if ($job.Weekly) { "毎週$($job.Weekly)" } else { '毎日' }
    Write-Host "登録しました: $($job.Name)  $schedule $($job.Time)  $($job.Desc)"
}

Write-Host ''
Write-Host '登録済みのタスク:'
Get-ScheduledTask -TaskName 'daily-companion-*' |
    Select-Object TaskName, State |
    Format-Table -AutoSize
