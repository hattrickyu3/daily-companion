# claude setup-token で作った長期トークンを .env に書き込み、Webアプリを再起動する。
#
# 使い方: claude setup-token で表示された sk-ant-oat01- で始まる文字列をコピーしてから、
#         scripts\set_claude_token.bat をダブルクリックする。
# トークンは画面に表示しない。すでに書いてあれば新しいものに置き換える。

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'
$key = 'CLAUDE_CODE_OAUTH_TOKEN'

$clip = Get-Clipboard -Raw
if ($null -eq $clip) { $clip = '' }
# ターミナルからコピーすると途中で改行が入ることがあるので、空白はすべて取り除く。
$token = $clip -replace '\s', ''

if ($token -notmatch '^sk-ant-oat01-[A-Za-z0-9_\-]+$') {
    Write-Warning 'クリップボードにトークンが見つかりません。'
    Write-Host 'claude setup-token で表示された「sk-ant-oat01-」で始まる文字列をコピーしてから、もう一度実行してください。'
    exit 1
}

$lines = @()
if (Test-Path $envFile) {
    $lines = @(Get-Content -Path $envFile -Encoding UTF8 | Where-Object { $_ -notmatch "^\s*$key\s*=" })
}
$lines += "$key=$token"
# 元の .env がBOM付きUTF-8なので、それに合わせる。
[System.IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($true)))
Set-Clipboard -Value ' '
Write-Host '.env にトークンを書き込みました。（クリップボードは空にしました）'

Write-Host ''
Write-Host 'Webアプリを再起動します。「このアプリがデバイスに変更を加えることを許可しますか？」と出たら「はい」を押してください。'
$restart = Join-Path $PSScriptRoot 'restart_web.ps1'
try {
    $proc = Start-Process powershell -Verb RunAs -Wait -PassThru `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$restart`""
} catch {
    Write-Warning '再起動がキャンセルされました。あとで管理者PowerShellから scripts\restart_web.ps1 を実行してください。'
    exit 1
}
if ($proc.ExitCode -eq 0) {
    Write-Host 'Webアプリを再起動しました。これで完了です。'
} else {
    Write-Warning 'Webアプリの再起動に失敗しました。logs\web.log を確認してください。'
    exit 1
}
