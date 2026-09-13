-- Таблицы и представления бота поверх реестра школ (schools_chuvashia.sqlite).

CREATE TABLE IF NOT EXISTS municipalities (
    municipality_id   TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    short_name        TEXT NOT NULL,
    municipality_type TEXT NOT NULL CHECK (municipality_type IN ('city', 'okrug')),
    sort_order        INTEGER NOT NULL,
    source_id         TEXT,
    checked_at        TEXT
);

ALTER TABLE units ADD COLUMN municipality_id TEXT REFERENCES municipalities(municipality_id);
CREATE INDEX IF NOT EXISTS units_municipality_idx ON units(municipality_id);

-- Профили уведомлений (формат profile_id: telegram:chat:<chat_id>).
CREATE TABLE IF NOT EXISTS notification_profiles (
    profile_id      TEXT PRIMARY KEY,
    default_enabled INTEGER NOT NULL DEFAULT 1 CHECK (default_enabled IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE TABLE IF NOT EXISTS notification_region_settings (
    profile_id      TEXT NOT NULL REFERENCES notification_profiles(profile_id) ON DELETE CASCADE,
    municipality_id TEXT NOT NULL REFERENCES municipalities(municipality_id),
    enabled         INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (profile_id, municipality_id)
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id        INTEGER PRIMARY KEY,
    profile_id     TEXT NOT NULL UNIQUE REFERENCES notification_profiles(profile_id),
    username       TEXT,
    first_name     TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    mode           TEXT NOT NULL DEFAULT 'digest' CHECK (mode IN ('digest', 'instant')),
    digest_times   TEXT NOT NULL DEFAULT '13:00,21:00',
    interval_days  INTEGER NOT NULL DEFAULT 1 CHECK (interval_days BETWEEN 1 AND 30),
    digest_format  TEXT NOT NULL DEFAULT 'cards' CHECK (digest_format IN ('cards', 'list')),
    notify_empty   INTEGER NOT NULL DEFAULT 0 CHECK (notify_empty IN (0, 1)),
    timezone       TEXT NOT NULL DEFAULT 'Europe/Moscow',
    next_digest_at TEXT,
    last_digest_at TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    last_seen_at   TEXT
);
CREATE INDEX IF NOT EXISTS subscribers_next_digest_idx ON subscribers(next_digest_at) WHERE is_active = 1;

-- Разрешение коротких адресов и состояние опроса сообществ.
ALTER TABLE communities ADD COLUMN resolved_group_id INTEGER;
ALTER TABLE communities ADD COLUMN resolve_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE communities ADD COLUMN resolve_error TEXT;
ALTER TABLE communities ADD COLUMN resolved_at TEXT;
ALTER TABLE communities ADD COLUMN title TEXT;
ALTER TABLE communities ADD COLUMN is_closed INTEGER;
ALTER TABLE communities ADD COLUMN duplicate_of TEXT;
ALTER TABLE communities ADD COLUMN scan_enabled INTEGER NOT NULL DEFAULT 1 CHECK (scan_enabled IN (0, 1));
CREATE INDEX IF NOT EXISTS communities_resolved_idx ON communities(resolved_group_id);

CREATE TABLE IF NOT EXISTS community_cursors (
    community_key      TEXT PRIMARY KEY REFERENCES communities(community_key),
    last_post_id       INTEGER,
    last_post_date     TEXT,
    last_checked_at    TEXT,
    last_success_at    TEXT,
    last_error         TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE posts ADD COLUMN is_repost INTEGER NOT NULL DEFAULT 0;
ALTER TABLE posts ADD COLUMN attachments TEXT;
ALTER TABLE posts ADD COLUMN classified_at TEXT;
ALTER TABLE posts ADD COLUMN is_achievement INTEGER;
ALTER TABLE posts ADD COLUMN confidence REAL;
ALTER TABLE posts ADD COLUMN classifier_version TEXT;
ALTER TABLE posts ADD COLUMN keyword_hits TEXT;
CREATE INDEX IF NOT EXISTS posts_published_idx ON posts(published_at);
CREATE INDEX IF NOT EXISTS posts_community_idx ON posts(community_key);

ALTER TABLE achievement_candidates ADD COLUMN confidence REAL;
ALTER TABLE achievement_candidates ADD COLUMN kind TEXT;
ALTER TABLE achievement_candidates ADD COLUMN headline TEXT;
ALTER TABLE achievement_candidates ADD COLUMN students TEXT;
ALTER TABLE achievement_candidates ADD COLUMN community_key TEXT REFERENCES communities(community_key);
ALTER TABLE achievement_candidates ADD COLUMN municipality_ids TEXT;
ALTER TABLE achievement_candidates ADD COLUMN unit_ids TEXT;
ALTER TABLE achievement_candidates ADD COLUMN school_names TEXT;
ALTER TABLE achievement_candidates ADD COLUMN published_at TEXT;
ALTER TABLE achievement_candidates ADD COLUMN url TEXT;
ALTER TABLE achievement_candidates ADD COLUMN reviewed_by INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS achievement_candidates_post_idx ON achievement_candidates(owner_id, post_id);
CREATE INDEX IF NOT EXISTS achievement_candidates_published_idx ON achievement_candidates(published_at);

CREATE TABLE IF NOT EXISTS deliveries (
    chat_id    INTEGER NOT NULL REFERENCES subscribers(chat_id) ON DELETE CASCADE,
    owner_id   INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    sent_at    TEXT NOT NULL,
    message_id INTEGER,
    channel    TEXT NOT NULL CHECK (channel IN ('instant', 'digest', 'manual')),
    PRIMARY KEY (chat_id, owner_id, post_id)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id             INTEGER PRIMARY KEY,
    run_key            TEXT UNIQUE,
    kind               TEXT NOT NULL CHECK (kind IN ('window', 'manual')),
    window_start       TEXT NOT NULL,
    window_end         TEXT NOT NULL,
    total              INTEGER NOT NULL,
    next_index         INTEGER NOT NULL DEFAULT 0,
    posts_fetched      INTEGER NOT NULL DEFAULT 0,
    posts_new          INTEGER NOT NULL DEFAULT 0,
    achievements_found INTEGER NOT NULL DEFAULT 0,
    errors             INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'aborted')),
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);

-- Эффективные настройки территорий профиля: исключение либо общий режим профиля.
CREATE VIEW IF NOT EXISTS v_notification_regions AS
SELECT p.profile_id,
       m.municipality_id,
       m.name,
       m.short_name,
       m.municipality_type,
       m.sort_order,
       COALESCE(s.enabled, p.default_enabled) AS enabled,
       s.updated_at
FROM notification_profiles p
CROSS JOIN municipalities m
LEFT JOIN notification_region_settings s
       ON s.profile_id = p.profile_id AND s.municipality_id = m.municipality_id;

-- Сообщества, доступные профилю с учётом включённых территорий (только подтверждённые связи).
CREATE VIEW IF NOT EXISTS v_notification_targets AS
SELECT r.profile_id,
       c.community_key,
       c.canonical_url,
       c.screen_name,
       COALESCE(c.group_id, c.resolved_group_id) AS group_id,
       group_concat(DISTINCT u.unit_id)          AS unit_ids,
       group_concat(DISTINCT u.name)             AS school_names,
       group_concat(DISTINCT u.municipality_id)  AS municipality_ids
FROM communities c
JOIN unit_communities uc ON uc.community_key = c.community_key
JOIN units u ON u.unit_id = uc.unit_id
JOIN v_notification_regions r ON r.municipality_id = u.municipality_id AND r.enabled = 1
WHERE u.scope = 'school'
  AND uc.monitor_enabled = 1
  AND uc.official_status = 'official_source_link'
  AND c.scan_enabled = 1
  AND c.duplicate_of IS NULL
GROUP BY r.profile_id, c.community_key;
