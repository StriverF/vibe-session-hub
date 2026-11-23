CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    title TEXT,
    name TEXT,
    cwd TEXT,
    git_branch TEXT,
    first_user_message TEXT,
    preview TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    created_at TEXT,
    updated_at TEXT,
    archived INTEGER DEFAULT 0,
    rollout_path TEXT
);

INSERT INTO threads VALUES (
    'codex-thread-101',
    'Refactor billing retry queue',
    'Billing Retry Queue',
    '/Users/dev/workspace/billing-service',
    'feat/billing-retry-queue',
    'Add exponential backoff to webhook retries',
    '',
    1760600000000,
    1760603600000,
    '2026-10-16T08:00:00Z',
    '2026-10-16T09:00:00Z',
    0,
    ''
);

INSERT INTO threads VALUES (
    'codex-thread-102',
    'Rollout fallback audit',
    '',
    '/Users/dev/workspace/billing-service',
    'HEAD',
    'Inspect rollout logs for stale locks',
    'Checked lock timeout handling',
    1760601000000,
    1760602000000,
    '2026-10-16T08:16:40Z',
    '2026-10-16T08:33:20Z',
    1,
    '__ROLLOUT_PATH__'
);
