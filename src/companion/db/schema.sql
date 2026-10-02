-- daily-companion スキーマ
-- 冪等に実行できるよう、すべて IF NOT EXISTS で定義する。

-- 日記本体。1日1レコード。
-- raw_text はユーザーが書いた原文で、Claudeの構造化結果がどうであれ必ず無傷で残す。
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date  TEXT    NOT NULL UNIQUE,          -- YYYY-MM-DD
    raw_text    TEXT    NOT NULL,
    summary     TEXT,                             -- Claude生成。失敗時は NULL
    done        TEXT,                             -- JSON配列: やったこと
    insights    TEXT,                             -- JSON配列: 気づき
    carry_over  TEXT,                             -- 明日への申し送り
    mood        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_entries_date ON entries (entry_date DESC);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags (id)    ON DELETE CASCADE,
    PRIMARY KEY (entry_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_entry_tags_tag ON entry_tags (tag_id);

-- iCloudから取り込んだ予定のキャッシュ。真実の源はあくまでAppleカレンダー側。
CREATE TABLE IF NOT EXISTS calendar_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT    NOT NULL UNIQUE,          -- CalDAVのUID
    event_date  TEXT    NOT NULL,                 -- YYYY-MM-DD（開始日）
    end_date    TEXT,                             -- YYYY-MM-DD（最終日、含む）。単日なら NULL
    starts_at   TEXT,                             -- ISO8601。終日予定では NULL
    ends_at     TEXT,
    title       TEXT    NOT NULL,
    location    TEXT,
    is_all_day  INTEGER NOT NULL DEFAULT 0,
    synced_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_date ON calendar_events (event_date);

-- 朝のブリーフィング。1日1件。
CREATE TABLE IF NOT EXISTS briefings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT    NOT NULL UNIQUE,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ニュース要約。1日 × ジャンル数。
CREATE TABLE IF NOT EXISTS news_digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date TEXT    NOT NULL,
    topic       TEXT    NOT NULL,                 -- ai_tech / job_hunting など
    body        TEXT    NOT NULL,
    sources     TEXT,                             -- JSON配列: 出典URL
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (digest_date, topic)
);

CREATE TABLE IF NOT EXISTS weekly_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT    NOT NULL UNIQUE,           -- その週の月曜日 YYYY-MM-DD
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 定期ジョブの実行ログ。「今朝ちゃんと動いたか」を後から追えるようにする。
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name    TEXT    NOT NULL,
    status      TEXT    NOT NULL,                 -- success / failed
    message     TEXT,
    duration_ms INTEGER,
    ran_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_job_runs_name_time ON job_runs (job_name, ran_at DESC);

-- 全文検索。entries と外部コンテンツ連携し、トリガで同期する。
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5 (
    raw_text,
    summary,
    content = 'entries',
    content_rowid = 'id',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS entries_fts_insert AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts (rowid, raw_text, summary) VALUES (new.id, new.raw_text, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS entries_fts_delete AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts (entries_fts, rowid, raw_text, summary)
    VALUES ('delete', old.id, old.raw_text, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS entries_fts_update AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts (entries_fts, rowid, raw_text, summary)
    VALUES ('delete', old.id, old.raw_text, old.summary);
    INSERT INTO entries_fts (rowid, raw_text, summary) VALUES (new.id, new.raw_text, new.summary);
END;

-- 議事録。音声そのものは data/minutes/<id>/ にファイルで置き、ここには文字と状態だけを持つ。
-- status: recording（録音中）→ finishing（清書中）→ done / failed
CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    participants TEXT NOT NULL DEFAULT '',  -- 参加者の名前（「自分、田中、佐藤」）. 話者の聞き分けの人数にも使う
    started_at  TEXT NOT NULL,              -- ISO 8601（ローカル時刻・タイムゾーン付き）
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'recording',
    draft       TEXT NOT NULL DEFAULT '{}', -- 録音中にHaikuが書いている議事録（見出し→本文のJSON）
    final_text  TEXT,                       -- 終了後にAIが清書した議事録（Markdown）
    polished    INTEGER NOT NULL DEFAULT 0, -- final_text がAIの清書か（0なら清書に失敗して下書きを入れた）
    hand_edited INTEGER NOT NULL DEFAULT 0, -- 清書のあとに手で直したか（清書をやり直すと消えるので警告に使う）
    note_path   TEXT,                       -- 書き出したノートの場所
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 音声の切れ端1つ = 1行。seg_id はiPhone側で録り始めた時刻（エポックミリ秒）で、送り直しても同じ値になる。
-- status: pending（文字起こし待ち）/ done / failed
CREATE TABLE IF NOT EXISTS meeting_segments (
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    seg_id      INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    text        TEXT NOT NULL DEFAULT '',
    error       TEXT,
    drafted     INTEGER NOT NULL DEFAULT 0, -- Haikuの議事録に反映済みか
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meeting_id, seg_id)
);
