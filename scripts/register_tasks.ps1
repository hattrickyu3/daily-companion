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
    @{ Name = 'daily-companion-catchup'; Script = 'morning_catchup.bat'; Desc = '朝のブリーフィングの取りこぼしを起動時に拾う'; AtStartup = $true; BootDelay = 'PT5M' }
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
        if ($job.BootDelay) {
            # 起動直後はネットワークもuvの準備も整っていないので少し待つ。
            # Windows Updateが再起動を連続で挟む場合、待っている間に次の再起動が
            # 来て打ち切られるが、最後の起動の分が走れば目的は果たせる。
            $trigger.Delay = $job.BootDelay
        }
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

# 音声入力ツール（右Ctrlで話すとカーソルの位置に文字が入る）。
# 画面とキーボードを使うので、S4U（セッション0）ではなく、ログインしたときに本人の画面で動かす。
# pythonw で動かして黒い窓を出さない。
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (Test-Path $pythonw) {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m companion.dictation' -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    # Webアプリより先に立ち上がっても、送るときにつながれば動く。少し待ってから起こす。
    $trigger.Delay = 'PT30S'
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'daily-companion-dictation' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description '音声入力ツール（右Ctrl）' -Force | Out-Null
    Write-Host '登録しました: daily-companion-dictation  ログイン時  音声入力ツール（右Ctrl）'
} else {
    Write-Host "音声入力ツールは登録しませんでした（$pythonw がありません。先に uv sync を実行してください）"
}

Write-Host ''
Write-Host '登録済みのタスク:'
Get-ScheduledTask -TaskName 'daily-companion-*' |
    Select-Object TaskName, State |
    Format-Table -AutoSize
