# Webアプリの監視つき起動。
#
# タスクスケジューラから PC起動時に呼ばれる。直接実行しても動く。
# uvicornが落ちても待ってから起動し直すので、
# 一時的なエラーやメモリ不足で止まったまま放置されることがない。
#
# 停止するには、タスクスケジューラで daily-companion-web を「終了」するか、
# scripts\stop_web.ps1 を実行する。

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'logs'
$logFile = Join-Path $logDir 'web.log'
$restartDelaySec = 10

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Host $line
}

Write-Log 'run_web: 監視を開始します'

# 日本語が文字化けしないようにしておく。
$env:PYTHONIOENCODING = 'utf-8'

while ($true) {
    Write-Log 'uvicorn を起動します'

    Push-Location $root
    try {
        # 標準出力・標準エラーをそのままログへ流す。
        & uv run python -m companion.web.serve 2>&1 | ForEach-Object {
            Add-Content -Path $logFile -Value $_ -Encoding utf8
        }
        $code = $LASTEXITCODE
    } catch {
        Write-Log "起動時に例外: $_"
        $code = -1
    } finally {
        Pop-Location
    }

    Write-Log "uvicorn が終了しました (終了コード $code)。$restartDelaySec 秒後に再起動します"
    Start-Sleep -Seconds $restartDelaySec
}
