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
    @{ Name = 'daily-companion-web';     Script = 'run_web.ps1';        Desc = 'Webアプリの常駐'; AtStartup = $true; LongRunning = $true }
)

foreach ($job in $jobs) {
    $scriptPath = Join-Path $root "scripts\$($job.Script)"
    if (-not (Test-Path $scriptPath)) {
        throw "スクリプトが見つかりません: $scriptPath"
    }

    if ($job.Script -like '*.ps1') {
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
            -WorkingDirectory $root
    } else {
        $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
            -Argument "/c `"$scriptPath`"" -WorkingDirectory $root
    }

    if ($job.AtStartup) {
        $trigger = New-ScheduledTaskTrigger -AtStartup
    } elseif ($job.Weekly) {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $job.Weekly -At $job.Time
    } else {
        $trigger = New-ScheduledTaskTrigger -Daily -At $job.Time
    }

    # S4U: パスワードなしで、ログインしていなくても実行する。
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

    # StartWhenAvailable: PCが落ちていて実行時刻を逃した場合、起動後に追いかけて実行する。
    # 電源断が起きうる環境なので、取りこぼしを拾えるようにしておく。
    #
    # ExecutionTimeLimit: 常駐するWebアプリは時間で打ち切られては困るので0(無制限)。
    # 定期ジョブは暴走を防ぐため15分で打ち切る。
    $timeLimit = if ($job.LongRunning) { New-TimeSpan -Seconds 0 } else { New-TimeSpan -Minutes 15 }

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit $timeLimit `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $job.Desc -Force | Out-Null

    $schedule = if ($job.AtStartup) {
        'PC起動時'
    } elseif ($job.Weekly) {
        "毎週$($job.Weekly) $($job.Time)"
    } else {
        "毎日 $($job.Time)"
    }
    Write-Host "登録しました: $($job.Name)  $schedule  $($job.Desc)"
}

Write-Host ''
Write-Host '登録済みのタスク:'
Get-ScheduledTask -TaskName 'daily-companion-*' |
    Select-Object TaskName, State |
    Format-Table -AutoSize
