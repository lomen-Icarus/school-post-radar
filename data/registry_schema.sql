PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT,
    source_type TEXT NOT NULL,
    source_date TEXT,
    accessed_at TEXT,
    sha256 TEXT,
    notes TEXT
);
CREATE TABLE organizations (
    inn TEXT PRIMARY KEY CHECK(length(inn) IN (10,12)),
    ogrn TEXT,
    legal_name TEXT NOT NULL,
    legal_address TEXT,
    legal_entity_status TEXT NOT NULL DEFAULT 'not_verified'
);
CREATE TABLE licenses (
    license_id TEXT PRIMARY KEY,
    inn TEXT NOT NULL REFERENCES organizations(inn),
    license_number TEXT NOT NULL,
    license_status TEXT NOT NULL,
    supplement_status TEXT,
    source_id TEXT NOT NULL REFERENCES sources(source_id)
);
CREATE TABLE units (
    unit_id TEXT PRIMARY KEY,
    inn TEXT NOT NULL REFERENCES organizations(inn),
    parent_unit_id TEXT REFERENCES units(unit_id),
    name TEXT NOT NULL,
    settlement TEXT,
    settlement_type TEXT,
    area_source TEXT,
    address TEXT,
    category TEXT,
    unit_kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    basic_general INTEGER CHECK(basic_general IN (0,1)),
    secondary_general INTEGER CHECK(secondary_general IN (0,1)),
    exam_route TEXT NOT NULL DEFAULT 'not_verified',
    website TEXT,
    website_status TEXT,
    website_source_id TEXT REFERENCES sources(source_id),
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    checked_at TEXT,
    notes TEXT
);
CREATE INDEX units_settlement_idx ON units(settlement, area_source);
CREATE INDEX units_inn_idx ON units(inn);
CREATE TABLE programs (
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    education_level TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    PRIMARY KEY(unit_id, education_level)
);
CREATE TABLE communities (
    community_key TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    screen_name TEXT,
    group_id INTEGER CHECK(group_id > 0),
    vk_owner_id INTEGER CHECK(vk_owner_id < 0),
    activity_status TEXT NOT NULL DEFAULT 'not_checked',
    last_post_date TEXT,
    activity_source_id TEXT REFERENCES sources(source_id),
    checked_at TEXT,
    UNIQUE(group_id)
);
CREATE TABLE unit_communities (
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    community_key TEXT NOT NULL REFERENCES communities(community_key),
    official_status TEXT NOT NULL,
    evidence_source_id TEXT REFERENCES sources(source_id),
    evidence_text TEXT,
    monitor_enabled INTEGER NOT NULL DEFAULT 0 CHECK(monitor_enabled IN (0,1)),
    notes TEXT,
    PRIMARY KEY(unit_id,community_key)
);
CREATE TABLE review_items (
    review_id TEXT PRIMARY KEY,
    inn TEXT,
    unit_id TEXT REFERENCES units(unit_id),
    issue_type TEXT NOT NULL,
    name TEXT,
    details TEXT NOT NULL,
    source_id TEXT REFERENCES sources(source_id),
    resolution TEXT,
    resolved_at TEXT
);

-- Empty application tables: no student profiles or fabricated examples.
CREATE TABLE monitor_checks (
    check_id INTEGER PRIMARY KEY,
    community_key TEXT NOT NULL REFERENCES communities(community_key),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL,
    newest_post_id INTEGER,
    error_message TEXT
);
CREATE TABLE posts (
    owner_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    community_key TEXT NOT NULL REFERENCES communities(community_key),
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    url TEXT NOT NULL,
    text TEXT,
    content_hash TEXT,
    PRIMARY KEY(owner_id,post_id)
);
CREATE TABLE achievement_candidates (
    candidate_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    category TEXT,
    summary TEXT,
    classifier_version TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    FOREIGN KEY(owner_id,post_id) REFERENCES posts(owner_id,post_id)
);

CREATE VIEW v_monitor_targets AS
SELECT c.community_key, c.canonical_url, c.screen_name, c.group_id,
       c.vk_owner_id, c.activity_status, c.last_post_date,
       group_concat(uc.unit_id, '; ') AS unit_ids,
       group_concat(u.name, '; ') AS school_names
FROM communities c
JOIN unit_communities uc USING(community_key)
JOIN units u USING(unit_id)
WHERE uc.monitor_enabled=1 AND u.scope='school'
GROUP BY c.community_key;

CREATE VIEW v_registry AS
SELECT u.*, o.ogrn, o.legal_name, o.legal_address,
       l.license_number, l.license_status, l.supplement_status
FROM units u JOIN organizations o USING(inn)
LEFT JOIN licenses l USING(inn);

CREATE VIEW v_settlement_list AS
SELECT settlement, settlement_type, area_source, unit_id, name, inn, address, website
FROM units WHERE scope='school'
ORDER BY settlement,area_source,name;
