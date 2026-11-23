CREATE TABLE thread_items (
    thread_id TEXT,
    rollout_ordinal INTEGER,
    item_type TEXT,
    item_json TEXT
);

INSERT INTO thread_items VALUES (
    'codex-thread-101',
    1,
    'userMessage',
    '{"content":[{"type":"input_text","text":"Add exponential backoff to webhook retries and link PR https://github.com/acme/billing-service/pull/2088"}]}'
);

INSERT INTO thread_items VALUES (
    'codex-thread-101',
    2,
    'agentMessage',
    '{"text":"Implemented bounded exponential backoff with jitter and passing retry tests."}'
);
