CREATE TABLE composerHeaders (
    composerId TEXT PRIMARY KEY,
    isSubagent INTEGER DEFAULT 0,
    value TEXT
);

CREATE TABLE cursorDiskKV (
    key TEXT PRIMARY KEY,
    value TEXT
);

INSERT INTO composerHeaders VALUES (
    'cursor-comp-001',
    0,
    '{"name":"Implement FTS5 trigram search","createdAt":1760700000000,"lastUpdatedAt":1760704000000,"trackedGitRepos":[{"repoPath":"/Users/dev/workspace/demo-app","branches":[{"branchName":"feat/fts5-search"}]}]}'
);

INSERT INTO composerHeaders VALUES (
    'cursor-sub-001',
    1,
    '{"name":"Subagent benchmark run","subagentParentId":"cursor-comp-001","branchName":"feat/fts5-bench","createdAt":1760701000000,"lastUpdatedAt":1760702000000}'
);

INSERT INTO composerHeaders VALUES (
    'cursor-draft-empty',
    0,
    '{"name":"Unsent empty draft","isDraft":true,"createdAt":1760705000000,"lastUpdatedAt":1760705000000}'
);

INSERT INTO cursorDiskKV VALUES (
    'composerData:cursor-comp-001',
    '{"name":"Implement FTS5 trigram search","fullConversationHeadersOnly":[{"bubbleId":"b1"}],"text":"Build FTS5 search with https://github.com/acme/demo-app/pull/3105"}'
);

INSERT INTO cursorDiskKV VALUES (
    'composerData:cursor-sub-001',
    '{"subagentInfo":{"parentComposerId":"cursor-comp-001"},"text":"Subagent verified https://github.com/acme/demo-app/pull/3106"}'
);

INSERT INTO cursorDiskKV VALUES (
    'composerData:cursor-draft-empty',
    '{"isDraft":true,"fullConversationHeadersOnly":[]}'
);

INSERT INTO cursorDiskKV VALUES (
    'composerData:bc-agent-777',
    '{"name":"Background CI flake triage","createdAt":1760706000000,"lastUpdatedAt":1760707000000,"branch":"fix/ci-flake","workspaceUri":"file:///Users/dev/workspace/demo-app","fullConversationHeadersOnly":[{"bubbleId":"b2"}]}'
);
