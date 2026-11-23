CREATE TABLE conversation_summaries (
    conversation_id TEXT PRIMARY KEY,
    title TEXT,
    preview TEXT,
    workspace_uris TEXT,
    last_modified_time TEXT
);

CREATE TABLE conversations_fts (
    conversation_id TEXT,
    user_body TEXT,
    agent_body TEXT
);

-- Duplicate ID already present in primary.sql (must be deduplicated)
INSERT INTO conversation_summaries VALUES (
    'agy-conv-001',
    'Duplicate lower-priority entry',
    'Should not overwrite primary entry',
    '["file:///Users/dev/workspace/other"]',
    '2026-10-18T10:00:00Z'
);

INSERT INTO conversation_summaries VALUES (
    'agy-conv-cli-002',
    'Verify headless CLI sync',
    'CLI sync verified',
    '["file:///Users/dev/workspace/demo-app"]',
    '2026-10-19T15:00:00Z'
);
