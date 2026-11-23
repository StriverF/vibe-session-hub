CREATE TABLE conversation_fts (
    rowid INTEGER PRIMARY KEY,
    body TEXT
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    branches TEXT,
    updated_at INTEGER,
    is_archived INTEGER DEFAULT 0,
    fts_rowid INTEGER
);

INSERT INTO conversation_fts VALUES (
    1,
    'How should we handle short 2-char CJK queries?\nFallback to indexed LIKE when query length < 3.'
);

INSERT INTO conversations VALUES (
    'cursor-search-only-002',
    'CJK short query fallback',
    'feat/cjk-fallback,HEAD',
    1760708000000,
    1,
    1
);
