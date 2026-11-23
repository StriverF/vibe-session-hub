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

INSERT INTO conversation_summaries VALUES (
    'agy-conv-001',
    'Design DMG release pipeline',
    'Added tag-triggered universal macOS DMG workflow',
    '["file:///Users/dev/workspace/My%20Demo%20App"]',
    '2026-10-19T14:30:00Z'
);

INSERT INTO conversations_fts VALUES (
    'agy-conv-001',
    'Build a universal macOS DMG installer for https://github.com/acme/demo-app/pull/6020',
    'Created scripts/package_macos.sh and .github/workflows/release.yml with SHA-256 verification.'
);
