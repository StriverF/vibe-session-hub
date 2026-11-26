from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import HubPaths

logger = logging.getLogger("vibe_session_hub.sync_mirror")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@contextlib.contextmanager
def try_sync_file_lock(lock_path: Path) -> Iterator[bool]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def trigger_icloud_downloads(folder: Path, raise_on_os_error: bool = False) -> None:
    try:
        if not folder.exists():
            return
        for item in folder.iterdir():
            if item.name.startswith(".") and item.name.endswith(".icloud"):
                try:
                    subprocess.Popen(
                        ["/usr/bin/brctl", "download", str(item)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as exc:
                    logger.debug("brctl download failed for %s: %s", item, exc)
    except OSError:
        if raise_on_os_error:
            raise
        return


class ICloudSyncMirror:
    """Manages local shard export, cross-process flock locking, and directional iCloud mirroring."""

    def __init__(
        self,
        paths: HubPaths,
        local_only: bool = False,
        trigger_downloads_fn: Callable[[Path, bool], None] | None = None,
    ) -> None:
        self.paths = paths
        self.local_only = local_only
        self._trigger_downloads = trigger_downloads_fn or trigger_icloud_downloads
        icloud_enabled = self.paths.is_icloud_enabled(local_only)
        self.icloud_access = "disabled" if not icloud_enabled else "init"
        self.icloud_status = "local_only" if not icloud_enabled else "init"
        self._last_export_digest = ""

    def active_sync_root(self) -> str:
        return (
            str(self.paths.icloud_sync_root)
            if self.paths.is_icloud_enabled(self.local_only)
            else str(self.paths.local_sync_root)
        )

    def probe_access(self) -> str:
        if not self.paths.is_icloud_enabled(self.local_only):
            self.icloud_access = "disabled"
            self.icloud_status = "local_only"
            return self.icloud_access
        try:
            (self.paths.icloud_sync_root / "nodes").mkdir(parents=True, exist_ok=True)
            (self.paths.icloud_sync_root / "manual_links").mkdir(
                parents=True, exist_ok=True
            )
            list((self.paths.icloud_sync_root / "nodes").iterdir())
            self.icloud_access = "direct"
        except OSError as exc:
            logger.debug("iCloud access probe denied: %s", exc)
            self.icloud_access = "permission_denied"
            if self.icloud_status != "synced_via_app":
                self.icloud_status = "local_fallback"
        return self.icloud_access

    def get_health(self) -> dict[str, Any]:
        if self.icloud_access == "init":
            self.probe_access()
        if self.local_only or self.icloud_access == "disabled":
            sync_owner = "none"
        elif self.icloud_access == "permission_denied":
            sync_owner = "app_bridge"
        else:
            sync_owner = "python"
        return {
            "local_only": self.local_only,
            "icloud_access": self.icloud_access,
            "icloud_status": self.icloud_status,
            "sync_owner": sync_owner,
        }

    def mark_bridge_synced(self) -> None:
        if self.paths.is_icloud_enabled(self.local_only):
            self.icloud_status = "synced_via_app"

    def export_local_shard(
        self,
        machine_id: str,
        machine_name: str,
        sessions: list[dict[str, Any]],
        machine_type: str = "",
        machine_model: str = "",
    ) -> bool:
        meta_payload: dict[str, Any] = {
            "_meta": True,
            "machine_id": machine_id,
            "machine_name": machine_name,
            "exported_at": int(time.time() * 1000),
        }
        if machine_type:
            meta_payload["machine_type"] = machine_type
        if machine_model:
            meta_payload["machine_model"] = machine_model

        lines = [json.dumps(meta_payload, ensure_ascii=False)]
        for s in sessions:
            s_copy = dict(s)
            s_copy["machine_name"] = machine_name
            if machine_type:
                s_copy["machine_type"] = machine_type
            lines.append(json.dumps(s_copy, ensure_ascii=False))

        # Hash content excluding exported_at so identical payloads don't churn disk or iCloud.
        payload_digest = hashlib.sha256(
            (
                "\n".join(
                    [machine_id, machine_name, machine_type, machine_model, *lines[1:]]
                )
            ).encode("utf-8")
        ).hexdigest()
        shard_path = self.paths.nodes_dir / f"{machine_id}.jsonl"
        if not self._last_export_digest and shard_path.is_file():
            try:
                existing_lines = shard_path.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
                if existing_lines:
                    meta = json.loads(existing_lines[0])
                    if (
                        isinstance(meta, dict)
                        and meta.get("_meta")
                        and meta.get("machine_id") == machine_id
                        and meta.get("machine_name") == machine_name
                        and str(meta.get("machine_type") or "") == machine_type
                        and str(meta.get("machine_model") or "") == machine_model
                    ):
                        self._last_export_digest = hashlib.sha256(
                            (
                                "\n".join(
                                    [
                                        machine_id,
                                        machine_name,
                                        machine_type,
                                        machine_model,
                                        *existing_lines[1:],
                                    ]
                                )
                            ).encode("utf-8")
                        ).hexdigest()
            except Exception as exc:
                logger.debug("Failed to read existing local shard %s: %s", shard_path, exc)
        if payload_digest == self._last_export_digest and shard_path.exists():
            return False

        blob = ("\n".join(lines) + "\n").encode("utf-8")
        atomic_write_bytes(shard_path, blob)
        self._last_export_digest = payload_digest
        return True

    def mirror(self, machine_id: str) -> bool:
        if not self.paths.is_icloud_enabled(self.local_only):
            self.icloud_access = "disabled"
            self.icloud_status = "local_only"
            return False

        lock_path = self.paths.local_sync_root / ".sync.lock"
        with try_sync_file_lock(lock_path) as acquired:
            if not acquired:
                logger.debug("Skipping iCloud mirror; lock busy at %s", lock_path)
                return False

            icloud_nodes = self.paths.icloud_sync_root / "nodes"
            icloud_links = self.paths.icloud_sync_root / "manual_links"
            changed = False
            try:
                icloud_nodes.mkdir(parents=True, exist_ok=True)
                icloud_links.mkdir(parents=True, exist_ok=True)
                self._trigger_downloads(icloud_nodes, True)
                self._trigger_downloads(icloud_links, True)

                own_node_name = f"{machine_id}.jsonl"
                own_links_name = f"{machine_id}-links.jsonl"

                changed |= self._sync_directory_scope(
                    self.paths.nodes_dir, icloud_nodes, own_node_name
                )
                changed |= self._sync_directory_scope(
                    self.paths.manual_links_dir, icloud_links, own_links_name
                )

                self.icloud_access = "direct"
                self.icloud_status = "synced"
                return changed
            except OSError as exc:
                logger.debug("iCloud mirror fell back due to OSError: %s", exc)
                self.icloud_access = "permission_denied"
                if self.icloud_status != "synced_via_app":
                    self.icloud_status = "local_fallback"
                return False

    @staticmethod
    def _sync_directory_scope(
        local_dir: Path, cloud_dir: Path, own_filename: str
    ) -> bool:
        changed = False
        # PUSH strictly this machine's own file (content-hashed + atomic write)
        local_own = local_dir / own_filename
        cloud_own = cloud_dir / own_filename
        if local_own.is_file():
            local_bytes = local_own.read_bytes()
            if sha256_bytes(local_bytes) != file_sha256(cloud_own):
                atomic_write_bytes(cloud_own, local_bytes)
                changed = True

        # PULL strictly remote machines' files
        for remote_file in sorted(cloud_dir.glob("*.jsonl")):
            if remote_file.name.startswith(".") or remote_file.name == own_filename:
                continue
            dest = local_dir / remote_file.name
            remote_bytes = remote_file.read_bytes()
            if sha256_bytes(remote_bytes) != file_sha256(dest):
                atomic_write_bytes(dest, remote_bytes)
                changed = True
        return changed
