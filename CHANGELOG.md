# Changelog

All notable changes to **Vibe Session Hub** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-11-23

### Added
- **Universal macOS App & DMG Packaging**: Standalone `Vibe Session Hub.app` (Apple Silicon + Intel Universal 2 binary, bundled Python runtime and static frontend, drag-to-`/Applications` `.dmg` installer with SHA-256 verification and one-click LaunchAgent menu actions).
- **Prism Aurora Default Dark Theme**: Logo-matched `prism-aurora` (星环极光) dark color scheme and 9 curated dark/light themes in a 3×3 appearance studio.
- **SQLite Schema v2 + FTS5 Trigram Search**: External Content FTS5 trigram full-text index with short-query CJK fallback, `session_prs` exact lookup table, and automatic lossless v1 → v2 migration.
- **Zero-Dependency CI/CD & Sanitized Collector Fixtures**: Full `unittest` coverage for all 7 client collectors (`Claude Magpie`, `Claude Desktop`, `Codex`, `Cursor`, `Gemini Desktop/CLI`, `Grok Bot`, `Antigravity`), path-traversal security, custom port propagation, and automated GitHub Releases publishing.

### Changed
- **Modular Architecture**: Split monolithic collectors, storage engine, launcher, and buildless frontend into focused modules backed by a single tool metadata registry (`hub/tool_registry.py`) and `HubPaths` dependency injection.
- **Atomic iCloud Ownership & WAL Concurrency**: Unified Python/Swift iCloud sync ownership via `/api/health` (`sync_owner`) and cross-process `.sync.lock` (`fcntl.flock` / BSD `O_EXLOCK`), with per-thread WAL read connections and singleflight PR lookups.

### Fixed
- **Static Path Validation (`is_relative_to()`)**: Hardened static file resolution against percent-encoded traversal, sibling directory prefix collisions, symlink escapes, NUL bytes, and backslash tricks, plus strict `Origin`/`Host` port validation on POST endpoints.
- **Custom Port End-to-End Propagation**: Propagated `VIBE_HUB_PORT` / `--port` / `preferences.json["port"]` consistently across the Python server, `/api/health`, `/api/facets`, frontend cross-Mac jump links, `VibeSessionHub.swift`, and `install.sh` LaunchAgent plist.
