from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import HubPaths
from .tool_registry import get_tool_label

logger = logging.getLogger("vibe_session_hub.shard_ingestor")

STANDALONE_SHARD_KEY = "__manual_links__"

SESSIONS_COLUMNS = (
    "uid",
    "session_id",
    "tool",
    "tool_label",
    "machine_id",
    "machine_name",
    "title",
    "first_prompt",
    "summary",
    "cwd",
    "branches_json",
    "branches_text",
    "prs_json",
    "pr_numbers",
    "pr_search_text",
    "created_at",
    "updated_at",
    "is_archived",
    "deep_link",
    "external_url",
    "search_text",
    "source_shard",
    "record_hash",
)

SESSIONS_INSERT_SQL = f"""
    INSERT INTO sessions ({", ".join(SESSIONS_COLUMNS)})
    VALUES ({", ".join("?" for _ in SESSIONS_COLUMNS)})
    ON CONFLICT(uid) DO UPDATE SET
        {", ".join(f"{c} = excluded.{c}" for c in SESSIONS_COLUMNS if c != "uid")}
"""

SESSIONS_UPDATE_SQL = f"""
    UPDATE sessions SET
        {", ".join(f"{c} = ?" for c in SESSIONS_COLUMNS if c != "uid")}
    WHERE uid = ?
"""


def load_manual_links(
    manual_links_dir: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    attachments: dict[str, list[dict[str, Any]]] = {}
    standalones: list[dict[str, Any]] = []
    if not manual_links_dir.exists():
        return attachments, standalones

    seen_ids: set[str] = set()
    for fp in sorted(manual_links_dir.glob("*.jsonl")):
        if fp.name.startswith("."):
            continue
        try:
            for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                lid = str(item.get("link_id") or "")
                if lid and lid in seen_ids:
                    continue
                if lid:
                    seen_ids.add(lid)
                if item.get("deleted"):
                    continue
                target_uid = str(item.get("target_uid") or "").strip()
                if target_uid:
                    attachments.setdefault(target_uid, []).append(item)
                else:
                    standalones.append(item)
        except Exception as exc:
            logger.warning("Skipping corrupt manual links file %s: %s", fp, exc)
            continue
    return attachments, standalones


def parse_node_shard(shard_path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    shard_machine_name = ""
    try:
        with shard_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("_meta"):
                    shard_machine_name = str(obj.get("machine_name") or "")
                    continue
                uid = str(obj.get("uid") or "")
                if not uid:
                    continue
                if shard_machine_name:
                    obj["machine_name"] = shard_machine_name
                records[uid] = obj
    except Exception as exc:
        logger.warning("Failed to parse node shard %s: %s", shard_path, exc)
        return {}
    return records


def apply_attachments(
    rec: dict[str, Any], extra_list: list[dict[str, Any]]
) -> dict[str, Any]:
    if not extra_list:
        return rec
    rec = dict(rec)
    prs = list(rec.get("prs") or [])
    notes: list[str] = []
    for ex in extra_list:
        pr_num = str(ex.get("pr_number") or "").strip()
        pr_url = str(ex.get("pr_url") or "").strip()
        if pr_num:
            prs.append({"number": pr_num, "repo": "", "url": pr_url, "branch": ""})
        if ex.get("external_url") and not rec.get("external_url"):
            rec["external_url"] = str(ex["external_url"])
        if ex.get("title"):
            rec["title"] = str(ex["title"])
            notes.append(str(ex["title"]))
        if ex.get("note"):
            notes.append(str(ex["note"]))
    dedup_prs: dict[str, dict[str, str]] = {}
    for p in prs:
        n = str(p.get("number") or "").strip()
        if n:
            if n not in dedup_prs or (p.get("url") and not dedup_prs[n].get("url")):
                dedup_prs[n] = {
                    "number": n,
                    "repo": str(p.get("repo") or ""),
                    "url": str(p.get("url") or ""),
                    "branch": str(p.get("branch") or ""),
                }
    rec["prs"] = list(dedup_prs.values())
    if notes:
        rec["search_text"] = " ".join(notes) + "\n" + str(rec.get("search_text") or "")
    return rec


def build_standalone_records(
    standalones: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for item in standalones:
        lid = str(item.get("link_id") or "")
        if not lid:
            continue
        mid = str(item.get("machine_id") or "shared")
        mname = str(item.get("machine_name") or "跨设备共享")
        tool = str(item.get("tool") or "web-link")
        tool_label = str(item.get("tool_label") or get_tool_label(tool, "外部会话链接"))
        pr_num = str(item.get("pr_number") or "").strip()
        pr_url = str(item.get("pr_url") or "").strip()
        prs = (
            [{"number": pr_num, "repo": "", "url": pr_url, "branch": ""}]
            if pr_num
            else []
        )
        branch = str(item.get("branch") or "").strip()
        title = str(
            item.get("title") or item.get("note") or item.get("external_url") or lid
        )
        note = str(item.get("note") or "")
        ts = int(item.get("created_at") or int(time.time() * 1000))
        uid = f"{mid}:{tool}:{lid}"
        records[uid] = {
            "uid": uid,
            "session_id": lid,
            "tool": tool,
            "tool_label": tool_label,
            "machine_id": mid,
            "machine_name": mname,
            "title": title,
            "first_prompt": note,
            "summary": str(item.get("external_url") or ""),
            "cwd": str(item.get("cwd") or ""),
            "branches": [branch] if branch else [],
            "prs": prs,
            "created_at": ts,
            "updated_at": ts,
            "is_archived": False,
            "deep_link": str(item.get("external_url") or ""),
            "external_url": str(item.get("external_url") or ""),
            "search_text": f"{title}\n{note}\n{branch}\n{pr_num}",
        }
    return records


def prepare_row(
    rec: dict[str, Any], source_shard: str
) -> tuple[tuple[Any, ...], str, list[str]]:
    branches = [str(b) for b in (rec.get("branches") or []) if b]
    prs = [p for p in (rec.get("prs") or []) if isinstance(p, dict)]
    pr_nums_unique: list[str] = []
    seen_pr: set[str] = set()
    pr_search_parts: list[str] = []
    for p in prs:
        n = str(p.get("number") or "").strip()
        if n:
            if n not in seen_pr:
                seen_pr.add(n)
                pr_nums_unique.append(n)
            url = str(p.get("url") or "").strip()
            pr_search_parts.append(f"#{n} {n} {url}".strip())

    pr_pipe = ("|" + "|".join(pr_nums_unique) + "|") if pr_nums_unique else ""
    pr_search_text = " ".join(pr_search_parts)
    branches_text = " ".join(branches)
    branches_json = json.dumps(branches, ensure_ascii=False)
    prs_json = json.dumps(prs, ensure_ascii=False)

    core_values = (
        str(rec["uid"]),
        str(rec.get("session_id") or ""),
        str(rec.get("tool") or ""),
        str(rec.get("tool_label") or ""),
        str(rec.get("machine_id") or ""),
        str(rec.get("machine_name") or ""),
        str(rec.get("title") or ""),
        str(rec.get("first_prompt") or ""),
        str(rec.get("summary") or ""),
        str(rec.get("cwd") or ""),
        branches_json,
        branches_text,
        prs_json,
        pr_pipe,
        pr_search_text,
        int(rec.get("created_at") or 0),
        int(rec.get("updated_at") or 0),
        1 if rec.get("is_archived") else 0,
        str(rec.get("deep_link") or ""),
        str(rec.get("external_url") or ""),
        str(rec.get("search_text") or ""),
        source_shard,
    )
    record_hash = hashlib.sha256(
        json.dumps(core_values, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    full_row = (*core_values, record_hash)
    return full_row, record_hash, pr_nums_unique


def scan_disk_shards(
    nodes_dir: Path, manual_links_dir: Path
) -> tuple[dict[str, tuple[int, int]], dict[str, Path]]:
    curr_state: dict[str, tuple[int, int]] = {}
    node_paths: dict[str, Path] = {}
    if nodes_dir.exists():
        for p in sorted(nodes_dir.glob("*.jsonl")):
            if p.name.startswith("."):
                continue
            try:
                st = p.stat()
                key = f"node:{p.name}"
                curr_state[key] = (st.st_mtime_ns, st.st_size)
                node_paths[key] = p
            except OSError:
                continue
    if manual_links_dir.exists():
        for p in sorted(manual_links_dir.glob("*.jsonl")):
            if p.name.startswith("."):
                continue
            try:
                st = p.stat()
                curr_state[f"link:{p.name}"] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
    return curr_state, node_paths


class ShardIngestor:
    """Incrementally ingests node shards and manual links into SQLite with row-level SHA-256 diffing."""

    def __init__(
        self,
        paths: HubPaths,
        get_read_conn: Callable[[], sqlite3.Connection],
        write_conn: sqlite3.Connection,
        db_write_lock: threading.Lock,
        parse_node_shard_fn: Callable[[Path], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.paths = paths
        self._get_read_conn = get_read_conn
        self._write_conn = write_conn
        self._db_write_lock = db_write_lock
        self._parse_node_shard = parse_node_shard_fn or parse_node_shard

    def append_manual_link(
        self, data: dict[str, Any], machine_info: dict[str, Any]
    ) -> tuple[dict[str, Any], Path]:
        mid = machine_info["machine_id"]
        lid = f"link_{int(time.time() * 1000)}"
        raw_ext = str(data.get("external_url") or "").strip()
        safe_ext = raw_ext if raw_ext.startswith(("https://", "http://")) else ""
        tool = str(data.get("tool") or "grok-web").strip()
        tool_label = str(data.get("tool_label") or "").strip() or get_tool_label(
            tool, "Grok / Web"
        )
        entry = {
            "link_id": lid,
            "target_uid": str(data.get("target_uid") or "").strip(),
            "pr_number": re.sub(r"[^0-9]", "", str(data.get("pr_number") or "")),
            "pr_url": str(data.get("pr_url") or "").strip(),
            "external_url": safe_ext,
            "title": str(data.get("title") or "").strip(),
            "branch": str(data.get("branch") or "").strip(),
            "note": str(data.get("note") or "").strip(),
            "tool": tool,
            "tool_label": tool_label,
            "machine_id": mid,
            "machine_name": machine_info["machine_name"],
            "created_at": int(time.time() * 1000),
        }
        link_file = self.paths.manual_links_dir / f"{mid}-links.jsonl"
        self.paths.manual_links_dir.mkdir(parents=True, exist_ok=True)
        with link_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry, link_file

    def ingest(
        self,
        force_reindex: bool = False,
        dirty_shards: set[str] | None = None,
    ) -> bool:
        read_conn = self._get_read_conn()
        prev_state: dict[str, tuple[int, int]] = {
            str(r["shard_key"]): (int(r["mtime_ns"]), int(r["size"]))
            for r in read_conn.execute(
                "SELECT shard_key, mtime_ns, size FROM shard_state"
            ).fetchall()
        }
        curr_state, node_paths = scan_disk_shards(
            self.paths.nodes_dir, self.paths.manual_links_dir
        )
        dirty = set(dirty_shards or set())

        if not force_reindex and not dirty and curr_state == prev_state:
            return False

        curr_node_keys = {k for k in curr_state if k.startswith("node:")}
        prev_node_keys = {k for k in prev_state if k.startswith("node:")}
        removed_node_keys = prev_node_keys - curr_node_keys

        changed_node_keys: set[str] = {
            k
            for k in curr_node_keys
            if force_reindex or k in dirty or prev_state.get(k) != curr_state.get(k)
        }

        link_keys_union = {
            k for k in (set(prev_state) | set(curr_state)) if k.startswith("link:")
        }
        links_changed = (
            force_reindex
            or any(k in dirty for k in link_keys_union)
            or any(prev_state.get(k) != curr_state.get(k) for k in link_keys_union)
        )

        # Load manual links outside _db_write_lock
        attachments, standalones = load_manual_links(self.paths.manual_links_dir)

        if links_changed:
            changed_node_keys.update(
                self._find_link_affected_shards(read_conn, attachments, curr_node_keys)
            )

        # Parse ONLY changed shards outside _db_write_lock
        prepared_by_shard: dict[
            str, dict[str, tuple[tuple[Any, ...], str, list[str]]]
        ] = {}
        new_attached_targets: dict[str, str] = {}

        for shard_key in sorted(changed_node_keys):
            shard_path = node_paths.get(shard_key)
            if not shard_path or not shard_path.is_file():
                continue
            raw_records = self._parse_node_shard(shard_path)
            shard_prepared: dict[str, tuple[tuple[Any, ...], str, list[str]]] = {}
            for uid, rec in raw_records.items():
                extra = attachments.get(uid)
                if extra:
                    rec = apply_attachments(rec, extra)
                    new_attached_targets[uid] = shard_key
                shard_prepared[uid] = prepare_row(rec, shard_key)
            prepared_by_shard[shard_key] = shard_prepared

        if links_changed:
            standalone_recs = build_standalone_records(standalones)
            prepared_by_shard[STANDALONE_SHARD_KEY] = {
                uid: prepare_row(rec, STANDALONE_SHARD_KEY)
                for uid, rec in standalone_recs.items()
            }

        self._commit_prepared_shards(
            force_reindex=force_reindex,
            curr_node_keys=curr_node_keys,
            removed_node_keys=removed_node_keys,
            prepared_by_shard=prepared_by_shard,
            new_attached_targets=new_attached_targets,
            prev_state=prev_state,
            curr_state=curr_state,
        )
        return True

    @staticmethod
    def _find_link_affected_shards(
        read_conn: sqlite3.Connection,
        attachments: dict[str, list[dict[str, Any]]],
        curr_node_keys: set[str],
    ) -> set[str]:
        prev_attached_rows = read_conn.execute(
            "SELECT uid, source_shard FROM manual_link_targets"
        ).fetchall()
        affected: set[str] = {
            str(r["source_shard"])
            for r in prev_attached_rows
            if str(r["source_shard"]) in curr_node_keys
        }
        if attachments:
            target_uids = list(attachments.keys())
            placeholders = ",".join("?" for _ in target_uids)
            rows = read_conn.execute(
                f"SELECT uid, source_shard FROM sessions WHERE uid IN ({placeholders})",
                target_uids,
            ).fetchall()
            for r in rows:
                ss = str(r["source_shard"])
                if ss in curr_node_keys:
                    affected.add(ss)
            for tuid in target_uids:
                if ":" in tuid:
                    inferred = f"node:{tuid.split(':', 1)[0]}.jsonl"
                    if inferred in curr_node_keys:
                        affected.add(inferred)
        return affected

    def _commit_prepared_shards(
        self,
        *,
        force_reindex: bool,
        curr_node_keys: set[str],
        removed_node_keys: set[str],
        prepared_by_shard: dict[
            str, dict[str, tuple[tuple[Any, ...], str, list[str]]]
        ],
        new_attached_targets: dict[str, str],
        prev_state: dict[str, tuple[int, int]],
        curr_state: dict[str, tuple[int, int]],
    ) -> None:
        with self._db_write_lock:
            conn = self._write_conn

            if force_reindex:
                existing_shards = {
                    str(r["source_shard"])
                    for r in conn.execute(
                        "SELECT DISTINCT source_shard FROM sessions"
                    ).fetchall()
                }
                for orphan in existing_shards - curr_node_keys - {STANDALONE_SHARD_KEY}:
                    removed_node_keys.add(orphan)

            for rem_key in removed_node_keys:
                conn.execute(
                    "DELETE FROM session_prs WHERE uid IN (SELECT uid FROM sessions WHERE source_shard = ?)",
                    (rem_key,),
                )
                conn.execute("DELETE FROM sessions WHERE source_shard = ?", (rem_key,))
                conn.execute("DELETE FROM shard_state WHERE shard_key = ?", (rem_key,))
                conn.execute(
                    "DELETE FROM manual_link_targets WHERE source_shard = ?", (rem_key,)
                )

            for shard_key, prepared in prepared_by_shard.items():
                existing_hashes: dict[str, str] = {
                    str(r["uid"]): str(r["record_hash"])
                    for r in conn.execute(
                        "SELECT uid, record_hash FROM sessions WHERE source_shard = ?",
                        (shard_key,),
                    ).fetchall()
                }
                incoming_uids = set(prepared.keys())
                existing_uids = set(existing_hashes.keys())

                to_delete = list(existing_uids - incoming_uids)
                if to_delete:
                    conn.executemany(
                        "DELETE FROM session_prs WHERE uid = ?",
                        [(u,) for u in to_delete],
                    )
                    conn.executemany(
                        "DELETE FROM sessions WHERE uid = ?",
                        [(u,) for u in to_delete],
                    )

                to_insert: list[tuple[Any, ...]] = []
                to_update: list[tuple[Any, ...]] = []
                prs_to_clear: list[tuple[str]] = []
                prs_to_insert: list[tuple[str, str]] = []

                for uid, (row_vals, rec_hash, pr_list) in prepared.items():
                    old_hash = existing_hashes.get(uid)
                    if old_hash is None:
                        to_insert.append(row_vals)
                        prs_to_clear.append((uid,))
                        for pn in pr_list:
                            prs_to_insert.append((pn, uid))
                    elif old_hash != rec_hash:
                        to_update.append((*row_vals[1:], uid))
                        prs_to_clear.append((uid,))
                        for pn in pr_list:
                            prs_to_insert.append((pn, uid))

                if to_insert:
                    conn.executemany(SESSIONS_INSERT_SQL, to_insert)
                if to_update:
                    conn.executemany(SESSIONS_UPDATE_SQL, to_update)
                if prs_to_clear:
                    conn.executemany(
                        "DELETE FROM session_prs WHERE uid = ?", prs_to_clear
                    )
                if prs_to_insert:
                    conn.executemany(
                        "INSERT OR IGNORE INTO session_prs (pr_number, uid) VALUES (?, ?)",
                        prs_to_insert,
                    )

                if shard_key.startswith("node:"):
                    conn.execute(
                        "DELETE FROM manual_link_targets WHERE source_shard = ?",
                        (shard_key,),
                    )
                    shard_targets = [
                        (u, s)
                        for u, s in new_attached_targets.items()
                        if s == shard_key
                    ]
                    if shard_targets:
                        conn.executemany(
                            """
                            INSERT INTO manual_link_targets (uid, source_shard)
                            VALUES (?, ?)
                            ON CONFLICT(uid) DO UPDATE SET source_shard = excluded.source_shard
                            """,
                            shard_targets,
                        )

            removed_state_keys = set(prev_state.keys()) - set(curr_state.keys())
            if removed_state_keys:
                conn.executemany(
                    "DELETE FROM shard_state WHERE shard_key = ?",
                    [(k,) for k in removed_state_keys],
                )
            if curr_state:
                conn.executemany(
                    """
                    INSERT INTO shard_state (shard_key, mtime_ns, size)
                    VALUES (?, ?, ?)
                    ON CONFLICT(shard_key) DO UPDATE SET
                        mtime_ns = excluded.mtime_ns,
                        size = excluded.size
                    """,
                    [(k, v[0], v[1]) for k, v in curr_state.items()],
                )

            conn.commit()
