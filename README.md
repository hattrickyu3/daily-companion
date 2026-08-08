# daily-companion

個人用の日記兼サポート係。予定とその日の行動をローカルのSQLiteに集約し、Claudeが毎朝・毎晩・毎週それを読んで話しかけてきます。

- **言語 / 環境**: Python 3.13 / [uv](https://docs.astral.sh/uv/)
- **DB**: SQLite（`data/companion.db`）
- **AI**: `claude -p` ヘッドレス実行
- **通知**: [ntfy.sh](https://ntfy.sh) によるスマホPush
- **カレンダー**: Googleカレンダー（Calendar API / サービスアカウント認証）

詳細は [要件定義書](docs/requirements.md) を参照。

## できること

| 時刻 | 動作 |
|---|---|
| 毎朝 07:30 | 予定の同期 → ニュース収集（AI・IT技術 / 就活・業界動向）→ ブリーフィング生成 → Push通知 |
| 毎晩 22:00 | 「今日は何した？」とPush通知。返信した内容をClaudeが構造化して保存 |
| 毎週日曜 21:00 | 1週間分の日記から傾向・やり残し・来週の提案をレポート（日記のない週は作らない） |

閲覧・入力は同じWi-Fi内のブラウザから（PC・スマホどちらでも）。

## セットアップ

```bash
uv sync
```

```bash
copy .env.example .env
```

`.env` に `NTFY_TOPIC` を記入し、Googleのサービスアカウント鍵を `credentials/service-account.json` に置いたあと、DBを初期化します。

```bash
uv run python -m companion.db.init
```

外部連携が正しく設定できているかを確認します。

```bash
uv run python -m companion.doctor
```

**管理者PowerShell**でタスクスケジューラに登録します。これでWebアプリの常駐と定期ジョブが動き出します。

```bash
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
```

登録されるタスク:

| タスク | タイミング | 内容 |
|---|---|---|
| `daily-companion-web` | PC起動時 | Webアプリの常駐（落ちたら10秒後に自動再起動） |
| `daily-companion-morning` | 毎日 07:30 | バックアップ → 予定同期 → ニュース → ブリーフィング |
| `daily-companion-evening` | 毎日 22:00 | 日記の催促（記録済みの日は送らない） |
| `daily-companion-weekly` | 毎週日曜 21:00 | 週次の振り返り |

いずれもS4Uログオンなので、Windowsにログインしていなくても動きます。
PCが落ちて実行時刻を逃した場合は、起動後に追いかけて実行されます。

手動で起動・停止・実行することもできます。

```bash
uv run python -m companion.jobs.morning_brief
```

```bash
powershell -ExecutionPolicy Bypass -File scripts\stop_web.ps1
```

## バックアップ

朝のジョブが毎日 `data/backups/companion-YYYYMMDD.db` を作り、7世代を保ちます。
SQLiteのオンラインバックアップAPIを使うので、書き込み中でも取りこぼしません。

戻すときは、Webアプリを止めてからファイルを差し替えます。

```bash
copy data\backups\companion-20260808.db data\companion.db
```

## 開発状況

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | 環境構築・DB・疎通確認 | 完了 |
| 1 | 日記の保存と閲覧 | 完了 |
| 2 | カレンダー同期と朝のブリーフィング | 完了 |
| 3 | ニュース要約 | 完了 |
| 4 | 週次レポート・全文検索 | 完了 |
| 5 | 自動バックアップ・常駐化 | 完了 |
