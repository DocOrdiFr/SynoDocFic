#!/usr/bin/env python3
"""Syno Batch Renamer backend for DSM 7.

Features:
- Preview rename operations
- Safe two-phase rename execution
- Conflict prevention with auto-generated unique names
- Undo for previous batches
- JSONL operation logging and history storage
- Synchronize folders (local, SMB, FTP)
"""

import argparse
import ftplib
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

MAX_PREVIEW_ITEMS = 20000
MAX_CACHE_PREVIEWS = 50
ALLOWED_ROOT_PREFIX = "/volume"

# Files/dirs to exclude from sync and compare
_SKIP_FILENAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store"})
_SKIP_DIRNAMES  = frozenset({"@eadir"})  # Synology thumbnail cache


def _sync_skip_name(name: str) -> bool:
    return name.lower() in _SKIP_FILENAMES


def _sync_skip_dir(name: str) -> bool:
    lower = name.lower()
    # Skip Synology EA dirs and *.mp3 pseudo-dirs (embedded album art)
    return lower in _SKIP_DIRNAMES or lower.endswith(".mp3")


def _sync_skip_rel(rel: str) -> bool:
    """True if any component of the relative path matches an exclusion rule."""
    for i, part in enumerate(rel.replace("\\", "/").split("/")):
        lower = part.lower()
        if lower in _SKIP_FILENAMES:
            return True
        if lower in _SKIP_DIRNAMES:
            return True
        if lower.endswith(".mp3"):  # parent dir = embedded art container
            return True
    return False


class AppState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.log_dir = data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = data_dir / "history.json"
        self.sync_profiles_file = data_dir / "sync_profiles.json"
        self.operations_log_file = self.log_dir / "operations.log"
        self.preview_cache = {}
        self.preview_order = deque()
        self.lock = threading.Lock()

        if not self.history_file.exists():
            self._write_history([])
        if not self.sync_profiles_file.exists():
            self._write_sync_profiles([])

    def _read_history(self):
        try:
            with self.history_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _write_history(self, history):
        temp_file = self.history_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=True, indent=2)
        os.replace(temp_file, self.history_file)

    def append_history(self, record):
        with self.lock:
            history = self._read_history()
            history.append(record)
            history = history[-100:]
            self._write_history(history)

    def mark_undone(self, batch_id, undo_summary):
        with self.lock:
            history = self._read_history()
            for item in history:
                if item.get("batch_id") == batch_id:
                    item["undone"] = True
                    item["undo_summary"] = undo_summary
                    item["undo_time_utc"] = now_utc_iso()
                    break
            self._write_history(history)

    def get_history(self, limit=20):
        with self.lock:
            history = self._read_history()
        return list(reversed(history[-limit:]))

    def cache_preview(self, preview_id, payload):
        with self.lock:
            self.preview_cache[preview_id] = payload
            self.preview_order.append(preview_id)
            while len(self.preview_order) > MAX_CACHE_PREVIEWS:
                old = self.preview_order.popleft()
                self.preview_cache.pop(old, None)

    def get_preview(self, preview_id):
        with self.lock:
            return self.preview_cache.get(preview_id)

    def _read_sync_profiles(self):
        try:
            with self.sync_profiles_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _write_sync_profiles(self, profiles):
        temp_file = self.sync_profiles_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=True, indent=2)
        os.replace(temp_file, self.sync_profiles_file)

    def get_sync_profiles(self):
        with self.lock:
            return self._read_sync_profiles()

    def add_sync_profile(self, profile_data):
        with self.lock:
            profiles = self._read_sync_profiles()
            profile = {
                "id": uuid.uuid4().hex,
                "name": profile_data.get("name", "Unnamed"),
                "source_protocol": profile_data.get("source_protocol", profile_data.get("protocol", "local")),
                "dest_protocol": profile_data.get("dest_protocol", profile_data.get("protocol", "local")),
                "source_host": profile_data.get("source_host", ""),
                "dest_host": profile_data.get("dest_host", ""),
                "source_path": profile_data.get("source_path", ""),
                "dest_path": profile_data.get("dest_path", ""),
                "source_user": profile_data.get("source_user", ""),
                "source_pass": profile_data.get("source_pass", ""),
                "dest_user": profile_data.get("dest_user", ""),
                "dest_pass": profile_data.get("dest_pass", ""),
                "recursive": profile_data.get("recursive", True),
                "created_at": now_utc_iso(),
            }
            profiles.append(profile)
            self._write_sync_profiles(profiles)
            return profile

    def update_sync_profile(self, profile_id: str, updates: dict) -> dict:
        with self.lock:
            profiles = self._read_sync_profiles()
            target = None
            for p in profiles:
                if p.get("id") == profile_id:
                    for key, val in updates.items():
                        p[key] = val
                    p["updated_at"] = now_utc_iso()
                    target = p
                    break
            if target is None:
                raise KeyError(f"Profile {profile_id} not found")
            self._write_sync_profiles(profiles)
            return target

    def delete_sync_profile(self, profile_id):
        with self.lock:
            profiles = self._read_sync_profiles()
            profiles = [p for p in profiles if p.get("id") != profile_id]
            self._write_sync_profiles(profiles)

    def append_operation_log(self, event):
        line = json.dumps(event, ensure_ascii=True)
        with self.lock:
            with self.operations_log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_valid_target_directory(path_str: str) -> bool:
    if not path_str:
        return False
    p = Path(path_str)
    if not p.is_absolute():
        return False
    normalized = p.as_posix()
    if not normalized.startswith(ALLOWED_ROOT_PREFIX):
        return False
    return p.exists() and p.is_dir()


def list_volume_roots():
    roots = []
    base = Path("/")
    for entry in base.iterdir():
        name = entry.name
        if entry.is_dir() and name.startswith("volume"):
            roots.append(entry.as_posix())
    return sorted(roots)


def list_subdirectories(path_str: str):
    dirs = []
    p = Path(path_str)
    with os.scandir(p) as it:
        for entry in it:
            if entry.is_dir(follow_symlinks=False):
                dirs.append(entry.path)
    return sorted(dirs, key=lambda s: s.lower())


def sanitize_transform_value(value: str) -> str:
    value = value or ""
    for forbidden in ("/", "\\", "\0"):
        value = value.replace(forbidden, "")
    return value


def split_name(filename: str):
    base, ext = os.path.splitext(filename)
    return base, ext


def transform_filename(original_name: str, mode: str, value: str) -> str:
    if mode == "remove":
        new_name = original_name.replace(value, "") if value else original_name
    elif mode == "prefix":
        new_name = f"{value}{original_name}"
    elif mode == "suffix":
        base, ext = split_name(original_name)
        new_name = f"{base}{value}{ext}"
    else:
        raise ValueError("Unsupported mode")

    if not new_name:
        new_name = "renamed_file"

    new_name = new_name.replace("/", "").replace("\\", "")
    if not new_name:
        new_name = "renamed_file"
    return new_name


def ensure_unique_name(candidate: str, used_names: set):
    if candidate not in used_names:
        return candidate, False

    base, ext = os.path.splitext(candidate)
    idx = 1
    while True:
        maybe = f"{base} ({idx}){ext}"
        if maybe not in used_names:
            return maybe, True
        idx += 1


def iter_target_files(root_dir: str, recursive: bool, limit: int):
    count = 0
    if recursive:
        queue = deque([root_dir])
        while queue:
            current = queue.popleft()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            queue.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield entry.path
                            count += 1
                            if count >= limit:
                                return
            except OSError:
                continue
    else:
        try:
            with os.scandir(root_dir) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False):
                        yield entry.path
                        count += 1
                        if count >= limit:
                            return
        except OSError:
            return


def build_preview_plan(directory: str, recursive: bool, mode: str, value: str, limit: int):
    raw_items = []
    for path_str in iter_target_files(directory, recursive, limit + 1):
        old_path = Path(path_str)
        old_name = old_path.name
        new_name = transform_filename(old_name, mode, value)
        raw_items.append(
            {
                "old_path": old_path.as_posix(),
                "old_name": old_name,
                "dir": old_path.parent.as_posix(),
                "candidate_name": new_name,
            }
        )

    truncated = len(raw_items) > limit
    if truncated:
        raw_items = raw_items[:limit]

    grouped = defaultdict(list)
    for item in raw_items:
        grouped[item["dir"]].append(item)

    plan = []
    summary = {
        "total_scanned": len(raw_items),
        "rename_actions": 0,
        "unchanged": 0,
        "auto_renamed": 0,
        "truncated": truncated,
    }

    for dir_path, items in grouped.items():
        existing = set()
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    existing.add(entry.name)
        except OSError:
            existing = set()

        old_names = {item["old_name"] for item in items}
        used = set(existing - old_names)

        # Stable order for deterministic conflict resolution.
        items_sorted = sorted(items, key=lambda i: i["old_name"].lower())

        for item in items_sorted:
            old_name = item["old_name"]
            candidate = item["candidate_name"]

            if candidate == old_name:
                plan.append(
                    {
                        "old_path": item["old_path"],
                        "new_path": f"{dir_path}/{old_name}",
                        "relative_old": os.path.relpath(item["old_path"], directory),
                        "relative_new": os.path.relpath(item["old_path"], directory),
                        "action": "skip",
                        "status": "unchanged",
                    }
                )
                summary["unchanged"] += 1
                used.add(old_name)
                continue

            final_name, auto_changed = ensure_unique_name(candidate, used)
            used.add(final_name)
            new_path = f"{dir_path}/{final_name}"

            status = "auto_renamed" if auto_changed else "ready"
            if auto_changed:
                summary["auto_renamed"] += 1

            plan.append(
                {
                    "old_path": item["old_path"],
                    "new_path": new_path,
                    "relative_old": os.path.relpath(item["old_path"], directory),
                    "relative_new": os.path.relpath(new_path, directory),
                    "action": "rename",
                    "status": status,
                }
            )
            summary["rename_actions"] += 1

    plan.sort(key=lambda i: i["relative_old"].lower())
    return plan, summary


def safe_json_body(handler: BaseHTTPRequestHandler):
    content_length = handler.headers.get("Content-Length", "0")
    try:
        size = int(content_length)
    except ValueError:
        size = 0

    raw = handler.rfile.read(size) if size > 0 else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def two_phase_rename(ops, batch_tag):
    staged = []
    finalized = []

    # Phase 1: move source files to temporary placeholders in same directory.
    for idx, op in enumerate(ops):
        src = op["old_path"]
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(f"Source file not found: {src}")

        temp_name = f".synobatch_tmp_{batch_tag}_{idx}"
        temp_path = src_path.with_name(temp_name)
        while temp_path.exists():
            temp_name = f".synobatch_tmp_{batch_tag}_{idx}_{uuid.uuid4().hex[:6]}"
            temp_path = src_path.with_name(temp_name)

        os.rename(src_path, temp_path)
        staged.append({"temp": temp_path.as_posix(), "target": op["new_path"], "source": src})

    # Phase 2: move placeholders to final targets.
    try:
        for item in staged:
            target_path = Path(item["target"])
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.rename(item["temp"], target_path)
            finalized.append(item)
    except Exception:
        # Rollback best effort.
        for item in reversed(finalized):
            try:
                os.rename(item["target"], item["source"])
            except OSError:
                pass
        for item in reversed(staged):
            temp_path = Path(item["temp"])
            if temp_path.exists():
                try:
                    os.rename(temp_path, item["source"])
                except OSError:
                    pass
        raise


def load_latest_not_undone(history_records):
    for item in reversed(history_records):
        if not item.get("undone"):
            return item
    return None


def build_undo_ops(batch_record):
    entries = batch_record.get("entries", [])
    per_dir = defaultdict(list)

    for e in entries:
        if e.get("status") != "renamed":
            continue
        current = e.get("to")
        desired = e.get("from")
        if not current or not desired:
            continue
        current_path = Path(current)
        desired_path = Path(desired)
        per_dir[current_path.parent.as_posix()].append(
            {
                "old_path": current_path.as_posix(),
                "desired_name": desired_path.name,
                "dir": current_path.parent.as_posix(),
                "original_from": desired_path.as_posix(),
            }
        )

    undo_ops = []
    for dir_path, items in per_dir.items():
        existing = set()
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    existing.add(entry.name)
        except OSError:
            existing = set()

        old_names = {Path(i["old_path"]).name for i in items}
        used = set(existing - old_names)

        for item in sorted(items, key=lambda i: Path(i["old_path"]).name.lower()):
            desired_name = item["desired_name"]
            final_name, auto_changed = ensure_unique_name(desired_name, used)
            used.add(final_name)
            undo_ops.append(
                {
                    "old_path": item["old_path"],
                    "new_path": f"{dir_path}/{final_name}",
                    "status": "auto_renamed" if auto_changed else "ready",
                }
            )

    return undo_ops


def file_hash(file_path: str) -> str:
    """Calculate SHA256 hash of a file (first 1MB for speed)."""
    try:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
        return ""


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_listing_text(profile: dict) -> str:
    """Produce a plain-text diagnostic dump of source and destination file listings."""
    src_proto = profile.get("source_protocol", profile.get("protocol", "local"))
    dst_proto = profile.get("dest_protocol", profile.get("protocol", "local"))
    recursive = profile.get("recursive", True)

    sep = "-" * 110

    def section(label, proto, host, path, user, password):
        lines = [f"=== {label} [{proto.upper()}] : {path} ==="]
        try:
            files = _list_endpoint_files(proto, host, path, user, password, recursive)
        except Exception as exc:
            lines.append(f"ERREUR : {exc}")
            return lines, []
        lines.append(f"Fichiers listés : {len(files)}")
        lines.append(sep)
        lines.append(f"{'Chemin relatif':<70}  {'Taille (o)':>12}  Mtime UTC")
        lines.append(sep)
        for f in sorted(files, key=lambda x: x["rel"].lower()):
            lines.append(f"{f['rel']:<70}  {f['size']:>12}  {_fmt_ts(f.get('mtime', 0))}")
        lines.append("")
        return lines, files

    out = [
        "=== SynoDocFic – Listing brut pour diagnostic ===",
        f"Profil  : {profile.get('name', '?')}",
        f"Date    : {now_utc_iso()}",
        f"Source  : [{src_proto}] {profile.get('source_host', '')}:{profile.get('source_path', '')}",
        f"Dest    : [{dst_proto}] {profile.get('dest_host', '')}:{profile.get('dest_path', '')}",
        f"Récursif: {recursive}",
        "",
    ]

    src_lines, src_files = section(
        "SOURCE", src_proto,
        profile.get("source_host", ""), profile.get("source_path", ""),
        profile.get("source_user", ""), profile.get("source_pass", ""),
    )
    out.extend(src_lines)

    dst_lines, dst_files = section(
        "DESTINATION", dst_proto,
        profile.get("dest_host", ""), profile.get("dest_path", ""),
        profile.get("dest_user", ""), profile.get("dest_pass", ""),
    )
    out.extend(dst_lines)

    # Cross-reference
    dst_index = {f["rel"]: f for f in dst_files}
    out += [sep, "=== COMPARAISON CROISÉE ===", sep]
    out.append(f"{'Chemin relatif':<70}  {'Src taille':>12}  {'Dst taille':>12}  {'Src mtime':>22}  {'Dst mtime':>22}  État")
    out.append(sep)
    for sf in sorted(src_files, key=lambda x: x["rel"].lower()):
        rel = sf["rel"]
        df = dst_index.get(rel)
        if df is None:
            state = "MANQUANT EN DEST"
        elif sf["size"] != df["size"]:
            state = f"TAILLE DIFF  src={sf['size']}  dst={df['size']}"
        elif sf.get("mtime", 0) > 0 and df.get("mtime", 0) > 0 and sf["mtime"] > df["mtime"] + 2:
            state = f"SRC PLUS RÉCENT  delta={sf['mtime'] - df['mtime']:.1f}s"
        else:
            continue  # skip identical files — user only wants differences
        out.append(f"{rel:<70}  {sf['size']:>12}  {df['size'] if df else '-':>12}  "
                   f"{_fmt_ts(sf.get('mtime', 0)):>22}  {_fmt_ts(df['mtime'] if df else 0):>22}  {state}")
    out.append("")

    return "\n".join(out)


def compare_folders(profile: dict) -> dict:
    """Dry-run comparison between source and destination (local, FTP or SMB)."""
    src = profile.get("source_path", "").strip()
    dst = profile.get("dest_path", "").strip()
    src_proto = profile.get("source_protocol", profile.get("protocol", "local"))
    dst_proto = profile.get("dest_protocol", profile.get("protocol", "local"))
    recursive = profile.get("recursive", True)

    if not src or not dst:
        raise RuntimeError("Chemins source et destination requis.")

    result: dict = {"to_copy": [], "to_update": [], "up_to_date": 0, "errors": []}

    try:
        src_files = _list_endpoint_files(
            src_proto, profile.get("source_host", ""),
            src, profile.get("source_user", ""), profile.get("source_pass", ""), recursive
        )
    except Exception as exc:
        result["errors"].append(f"Source : {exc}")
        return result

    try:
        dst_files = _list_endpoint_files(
            dst_proto, profile.get("dest_host", ""),
            dst, profile.get("dest_user", ""), profile.get("dest_pass", ""), recursive
        )
    except Exception as exc:
        result["errors"].append(f"Destination : {exc}")
        for f in src_files:
            result["to_copy"].append({"rel": f["rel"], "size": f["size"]})
        return result

    dst_index = {f["rel"]: f for f in dst_files}

    for src_file in src_files:
        rel = src_file["rel"]
        if rel not in dst_index:
            result["to_copy"].append({"rel": rel, "size": src_file["size"]})
        else:
            dst_file = dst_index[rel]
            size_diff = src_file["size"] != dst_file["size"]
            # Only compare mtime when both endpoints provide reliable timestamps
            both_have_mtime = src_file.get("mtime", 0) > 0 and dst_file.get("mtime", 0) > 0
            mtime_newer = both_have_mtime and src_file["mtime"] > dst_file["mtime"] + 2
            if size_diff or mtime_newer:
                result["to_update"].append({
                    "rel": rel,
                    "src_size": src_file["size"],
                    "dst_size": dst_file["size"],
                    "reason": "taille différente" if size_diff else "plus récent",
                })
            else:
                result["up_to_date"] += 1

    return result


def _list_endpoint_files(protocol: str, host: str, path: str, user: str, password: str, recursive: bool) -> list:
    if protocol == "local":
        raw = list_local_files(path, recursive)
        return [{"rel": os.path.relpath(f["path"], path), "size": f["size"], "mtime": f["mtime"]} for f in raw]
    if protocol == "ftp":
        if not host:
            raise RuntimeError("Hôte FTP requis.")
        return list_ftp_files(host, path, user, password, recursive)
    if protocol == "smb":
        if not host:
            raise RuntimeError("Hôte SMB requis.")
        return list_smb_files(host, path, user, password, recursive)
    raise RuntimeError(f"Protocole {protocol} non supporté pour la comparaison.")


def list_ftp_dirs(host: str, path: str, user: str, password: str) -> dict:
    """List directories on a remote FTP server."""
    host_part = host.strip()
    port = 21
    if ":" in host_part and not host_part.startswith("["):
        h, _, p = host_part.rpartition(":")
        try:
            port = int(p)
            host_part = h
        except ValueError:
            pass

    ftp = ftplib.FTP()
    try:
        ftp.connect(host_part, port, timeout=10)
        ftp.login(user or "anonymous", password or "")
        try:
            ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.all_errors:
            pass
        ftp.encoding = "utf-8"

        if path and path != "/":
            try:
                ftp.cwd(path)
            except ftplib.error_perm:
                pass

        current = ftp.pwd()
        parent = None
        if current != "/":
            parent = current.rstrip("/").rsplit("/", 1)[0] or "/"

        dirs: list = []

        def parse_line(line: str) -> None:
            parts = line.split(None, 8)
            if len(parts) >= 9 and parts[0].startswith("d"):
                name = parts[8].strip()
                if name not in (".", ".."):
                    dirs.append(current.rstrip("/") + "/" + name)

        try:
            ftp.retrlines("LIST", parse_line)
        except Exception:
            # Fallback: MLSD
            try:
                for name, facts in ftp.mlsd():
                    if facts.get("type") == "dir" and name not in (".", ".."):
                        dirs.append(current.rstrip("/") + "/" + name)
            except Exception:
                pass

        ftp.quit()
        return {"path": current, "directories": sorted(dirs), "parent": parent}
    except Exception as exc:
        raise RuntimeError(f"Connexion FTP impossible : {exc}")


def _parse_ftp_host(host: str):
    host = host.strip()
    port = 21
    if ":" in host and not host.startswith("["):
        h, _, p = host.rpartition(":")
        try:
            port = int(p); host = h
        except ValueError:
            pass
    return host, port


def _ftp_mtime(modify: str) -> float:
    try:
        dt = datetime.strptime(modify[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def list_ftp_files(host: str, base_path: str, user: str, password: str, recursive: bool = True) -> list:
    """Recursively list files on an FTP server with size and mtime."""
    host_part, port = _parse_ftp_host(host)
    base = base_path or "/"
    entries: list = []

    ftp = ftplib.FTP()
    try:
        ftp.connect(host_part, port, timeout=30)
        ftp.login(user or "anonymous", password or "")
        # Ask server for UTF-8 filenames; fall back silently if unsupported
        try:
            ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.all_errors:
            pass
        ftp.encoding = "utf-8"

        def scan(dir_path: str) -> None:
            try:
                for name, facts in ftp.mlsd(dir_path, facts=["size", "modify", "type"]):
                    if name in (".", ".."):
                        continue
                    full = dir_path.rstrip("/") + "/" + name
                    if facts.get("type") == "dir":
                        if recursive and not _sync_skip_dir(name):
                            scan(full)
                    else:
                        rel = os.path.relpath(full, base)
                        if not _sync_skip_rel(rel):
                            entries.append({
                                "rel": rel,
                                "size": int(facts.get("size", 0)),
                                "mtime": _ftp_mtime(facts.get("modify", "")),
                            })
            except ftplib.all_errors:
                lines: list = []
                try:
                    ftp.retrlines(f"LIST {dir_path}", lines.append)
                except ftplib.all_errors:
                    return
                for line in lines:
                    parts = line.split(None, 8)
                    if len(parts) < 9:
                        continue
                    name = parts[8].strip()
                    if name in (".", ".."):
                        continue
                    full = dir_path.rstrip("/") + "/" + name
                    if parts[0].startswith("d"):
                        if recursive and not _sync_skip_dir(name):
                            scan(full)
                    else:
                        rel = os.path.relpath(full, base)
                        if _sync_skip_rel(rel):
                            continue
                        size, mtime = 0, 0.0
                        try:
                            size = ftp.size(full) or 0
                        except ftplib.all_errors:
                            pass
                        try:
                            mtime = _ftp_mtime(ftp.sendcmd(f"MDTM {full}").split()[-1])
                        except ftplib.all_errors:
                            pass
                        entries.append({
                            "rel": rel,
                            "size": size, "mtime": mtime,
                        })

        scan(base)
        ftp.quit()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Erreur FTP : {exc}")

    return entries


def list_smb_files(host: str, path: str, user: str, password: str, recursive: bool = True) -> list:
    """List files on an SMB share via smbclient."""
    import shutil as _shutil
    smbclient = _shutil.which("smbclient")
    if not smbclient:
        for cand in ("/usr/bin/smbclient", "/usr/syno/bin/smbclient", "/opt/bin/smbclient"):
            if os.path.exists(cand):
                smbclient = cand
                break
    if not smbclient:
        raise RuntimeError("smbclient introuvable. Montez le partage localement ou utilisez FTP.")

    # First path component = SMB share name
    stripped = path.lstrip("/").replace("\\", "/")
    parts = stripped.split("/", 1)
    share = parts[0]
    subpath = parts[1] if len(parts) > 1 else ""

    unc = f"//{host}/{share}"
    auth = f"{user}%{password}" if user else "guest%"
    cmds: list = []
    if subpath:
        cmds.append(f"cd {subpath}")
    if recursive:
        cmds.extend(["recurse ON", "ls"])
    else:
        cmds.append("ls")

    proc = subprocess.run(
        [smbclient, unc, "-U", auth, "-c", "; ".join(cmds)],
        capture_output=True, text=True, timeout=30,
    )

    entries = []
    base = "/" + subpath if subpath else "/"
    current_dir = base

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("\\"):  # directory context printed by recurse
            current_dir = "/" + line.lstrip("\\").replace("\\", "/")
            continue
        parts = line.rsplit(None, 5)
        if len(parts) < 6:
            continue
        name, attrs = parts[0].lstrip(), parts[1]
        if name in (".", "..") or "D" in attrs:
            continue
        try:
            size = int(parts[2])
        except ValueError:
            continue
        rel_path = os.path.join(current_dir.lstrip("/"), name) if current_dir != "/" else name
        rel = os.path.relpath("/" + rel_path, base)
        if not _sync_skip_rel(rel):
            entries.append({"rel": rel, "size": size, "mtime": 0.0})

    if proc.returncode != 0 and not entries:
        msg = next((l for l in proc.stdout.splitlines() if "NT_STATUS" in l), "") or proc.stderr
        raise RuntimeError(f"Erreur SMB : {msg.strip() or 'connexion échouée'}")

    return entries


def list_local_files(directory: str, recursive: bool = True):
    """List files in a local directory with basic info."""
    files = []
    if recursive:
        for root, dirs, filenames in os.walk(directory):
            dirs[:] = [d for d in dirs if not _sync_skip_dir(d)]
            for fname in filenames:
                if _sync_skip_name(fname):
                    continue
                full_path = os.path.join(root, fname)
                try:
                    stat = os.stat(full_path)
                    files.append({
                        "path": full_path,
                        "name": fname,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
                except OSError:
                    pass
    else:
        try:
            for entry in os.scandir(directory):
                if entry.is_file(follow_symlinks=False) and not _sync_skip_name(entry.name):
                    stat = entry.stat(follow_symlinks=False)
                    files.append({
                        "path": entry.path,
                        "name": entry.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
        except OSError:
            pass
    return files


def sync_folders(source_profile: dict, app_state: 'AppState') -> dict:
    """Synchronize folders unidirectionally from source to dest."""
    result = {
        "files_copied": 0,
        "files_skipped": 0,
        "errors": 0,
        "error_messages": [],
    }

    try:
        src = source_profile.get("source_path", "").strip()
        dst = source_profile.get("dest_path", "").strip()
        # Support both old single-protocol and new per-endpoint protocols
        src_protocol = source_profile.get("source_protocol", source_profile.get("protocol", "local"))
        dst_protocol = source_profile.get("dest_protocol", source_profile.get("protocol", "local"))
        recursive = source_profile.get("recursive", True)

        if not src or not dst:
            result["errors"] = 1
            result["error_messages"].append("Source or destination path empty")
            return result

        if src_protocol != "local":
            result["errors"] = 1
            result["error_messages"].append(f"Sync depuis {src_protocol.upper()} non encore implémenté. Montez le partage comme chemin local.")
            return result

        if dst_protocol != "local":
            result["errors"] = 1
            result["error_messages"].append(f"Sync vers {dst_protocol.upper()} non encore implémenté. Montez le partage comme chemin local.")
            return result

        # Create destination if it doesn't exist
        try:
            os.makedirs(dst, exist_ok=True)
        except OSError as e:
            result["errors"] = 1
            result["error_messages"].append(f"Cannot create destination: {str(e)}")
            return result

        # List files in source
        try:
            src_files = list_local_files(src, recursive)
        except Exception as e:
            result["errors"] = 1
            result["error_messages"].append(f"Cannot read source: {str(e)}")
            return result

        # For each file, check if it exists in destination with same size/mtime
        for src_file in src_files:
            rel_path = os.path.relpath(src_file["path"], src)
            dst_file_path = os.path.join(dst, rel_path)

            # Ensure destination directory exists
            dst_dir = os.path.dirname(dst_file_path)
            try:
                os.makedirs(dst_dir, exist_ok=True)
            except OSError:
                result["errors"] += 1
                result["error_messages"].append(f"Cannot create dir: {dst_dir}")
                continue

            # Check if file exists and is identical (size + mtime)
            if os.path.exists(dst_file_path):
                try:
                    dst_stat = os.stat(dst_file_path)
                    if dst_stat.st_size == src_file["size"] and dst_stat.st_mtime == src_file["mtime"]:
                        result["files_skipped"] += 1
                        continue
                except OSError:
                    pass

            # Copy file
            try:
                shutil.copy2(src_file["path"], dst_file_path)
                result["files_copied"] += 1
            except Exception as e:
                result["errors"] += 1
                result["error_messages"].append(f"Copy failed {rel_path}: {str(e)}")

        return result

    except Exception as e:
        result["errors"] = 1
        result["error_messages"].append(f"Sync error: {str(e)}")
        return result


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "SynoBatchRenamer/1.0"

    def _add_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _write_json(self, status_code: int, payload):
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _write_text_download(self, text: str, filename: str):
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, url_path: str):
        static_root = self.server.static_root
        cleaned = posixpath.normpath(unquote(url_path))
        if cleaned.startswith("/"):
            cleaned = cleaned[1:]
        if cleaned in ("", "."):
            cleaned = "index.html"

        fs_path = (static_root / cleaned).resolve()
        try:
            fs_path.relative_to(static_root.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        if not fs_path.exists() or not fs_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content = fs_path.read_bytes()
        mime = "text/plain; charset=utf-8"
        if fs_path.suffix == ".html":
            mime = "text/html; charset=utf-8"
        elif fs_path.suffix == ".css":
            mime = "text/css; charset=utf-8"
        elif fs_path.suffix == ".js":
            mime = "application/javascript; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._add_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/dirs":
            qs = parse_qs(parsed.query)
            path_arg = qs.get("path", [""])[0]

            if not path_arg:
                roots = list_volume_roots()
                return self._write_json(HTTPStatus.OK, {"path": "/", "directories": roots, "parent": None})

            if not is_valid_target_directory(path_arg):
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid directory. Use an absolute /volumeX path."})

            try:
                directories = list_subdirectories(path_arg)
                parent = str(Path(path_arg).parent)
                if parent == ".":
                    parent = "/"
                return self._write_json(
                    HTTPStatus.OK,
                    {
                        "path": path_arg,
                        "directories": directories,
                        "parent": parent,
                    },
                )
            except OSError as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = min(max(limit, 1), 100)
            history = self.server.state.get_history(limit=limit)
            return self._write_json(HTTPStatus.OK, {"items": history})

        if parsed.path == "/api/sync/profiles":
            # GET: List all sync profiles
            try:
                profiles = self.server.state.get_sync_profiles()
                return self._write_json(HTTPStatus.OK, {"profiles": profiles})
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if parsed.path == "/":
            return self._serve_static("index.html")

        if parsed.path.startswith("/static/"):
            return self._serve_static(parsed.path[len("/static/") :])

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        data = safe_json_body(self)

        if parsed.path == "/api/preview":
            directory = str(data.get("directory", "")).strip()
            recursive = bool(data.get("recursive", False))
            mode = str(data.get("mode", "")).strip().lower()
            value = sanitize_transform_value(str(data.get("value", "")))
            limit = int(data.get("limit", MAX_PREVIEW_ITEMS))
            limit = min(max(limit, 1), MAX_PREVIEW_ITEMS)

            if mode not in {"remove", "prefix", "suffix"}:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid mode."})
            if not is_valid_target_directory(directory):
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid directory. Use an absolute /volumeX path."})

            if mode == "remove" and not value:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Text to remove cannot be empty."})

            try:
                plan, summary = build_preview_plan(directory, recursive, mode, value, limit)
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

            preview_id = uuid.uuid4().hex
            payload = {
                "preview_id": preview_id,
                "created_at_utc": now_utc_iso(),
                "request": {
                    "directory": directory,
                    "recursive": recursive,
                    "mode": mode,
                    "value": value,
                    "limit": limit,
                },
                "plan": plan,
                "summary": summary,
            }
            self.server.state.cache_preview(preview_id, payload)

            return self._write_json(
                HTTPStatus.OK,
                {
                    "preview_id": preview_id,
                    "summary": summary,
                    "items": [
                        {
                            "from": p["relative_old"],
                            "to": p["relative_new"],
                            "action": p["action"],
                            "status": p["status"],
                        }
                        for p in plan
                    ],
                },
            )

        if parsed.path == "/api/rename":
            preview_id = str(data.get("preview_id", "")).strip()
            confirm = bool(data.get("confirm", False))
            if not preview_id or not confirm:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "preview_id and confirm=true are required."})

            cached = self.server.state.get_preview(preview_id)
            if not cached:
                return self._write_json(HTTPStatus.NOT_FOUND, {"error": "Preview not found or expired."})

            plan = cached["plan"]
            request_info = cached["request"]
            rename_ops = [p for p in plan if p.get("action") == "rename"]

            batch_id = uuid.uuid4().hex
            changed_entries = []

            try:
                two_phase_rename(rename_ops, batch_id[:8])
                for item in plan:
                    status = "renamed" if item.get("action") == "rename" else "unchanged"
                    changed_entries.append(
                        {
                            "from": item["old_path"],
                            "to": item["new_path"],
                            "status": status,
                        }
                    )
            except Exception as exc:
                self.server.state.append_operation_log(
                    {
                        "time_utc": now_utc_iso(),
                        "event": "rename_failed",
                        "preview_id": preview_id,
                        "error": str(exc),
                    }
                )
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

            batch_record = {
                "batch_id": batch_id,
                "created_at_utc": now_utc_iso(),
                "request": request_info,
                "entries": changed_entries,
                "undone": False,
            }
            self.server.state.append_history(batch_record)
            self.server.state.append_operation_log(
                {
                    "time_utc": now_utc_iso(),
                    "event": "rename_applied",
                    "batch_id": batch_id,
                    "request": request_info,
                    "renamed_count": len(rename_ops),
                }
            )

            return self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "batch_id": batch_id,
                    "renamed_count": len(rename_ops),
                    "unchanged_count": len(plan) - len(rename_ops),
                },
            )

        if parsed.path == "/api/undo":
            requested_batch_id = str(data.get("batch_id", "")).strip()
            history = self.server.state.get_history(limit=100)
            target = None

            if requested_batch_id:
                for item in history:
                    if item.get("batch_id") == requested_batch_id:
                        target = item
                        break
            else:
                target = load_latest_not_undone(list(reversed(history)))

            if not target:
                return self._write_json(HTTPStatus.NOT_FOUND, {"error": "No batch available to undo."})
            if target.get("undone"):
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "This batch is already undone."})

            undo_ops = build_undo_ops(target)
            if not undo_ops:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "No rename entries to undo for this batch."})

            undo_batch_id = uuid.uuid4().hex
            try:
                two_phase_rename(undo_ops, f"undo_{undo_batch_id[:8]}")
            except Exception as exc:
                self.server.state.append_operation_log(
                    {
                        "time_utc": now_utc_iso(),
                        "event": "undo_failed",
                        "batch_id": target.get("batch_id"),
                        "error": str(exc),
                    }
                )
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

            undo_summary = {
                "undo_batch_id": undo_batch_id,
                "undo_count": len(undo_ops),
            }
            self.server.state.mark_undone(target.get("batch_id"), undo_summary)
            self.server.state.append_operation_log(
                {
                    "time_utc": now_utc_iso(),
                    "event": "undo_applied",
                    "batch_id": target.get("batch_id"),
                    "undo_batch_id": undo_batch_id,
                    "undo_count": len(undo_ops),
                }
            )

            return self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "batch_id": target.get("batch_id"),
                    "undo_batch_id": undo_batch_id,
                    "undo_count": len(undo_ops),
                },
            )

        # Sync profiles endpoints
        if parsed.path == "/api/sync/profiles":
            # POST: Add new sync profile
            valid_protocols = {"local", "smb", "ftp"}
            src_proto = str(data.get("source_protocol", data.get("protocol", "local"))).lower()
            dst_proto = str(data.get("dest_protocol", data.get("protocol", "local"))).lower()
            profile_data = {
                "name": str(data.get("name", "")).strip(),
                "source_protocol": src_proto,
                "dest_protocol": dst_proto,
                "source_host": str(data.get("source_host", "")).strip(),
                "dest_host": str(data.get("dest_host", "")).strip(),
                "source_path": str(data.get("source_path", "")).strip(),
                "dest_path": str(data.get("dest_path", "")).strip(),
                "source_user": str(data.get("source_user", "")).strip(),
                "source_pass": str(data.get("source_pass", "")).strip(),
                "dest_user": str(data.get("dest_user", "")).strip(),
                "dest_pass": str(data.get("dest_pass", "")).strip(),
                "recursive": bool(data.get("recursive", True)),
            }

            if not profile_data["name"] or not profile_data["source_path"] or not profile_data["dest_path"]:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Missing required fields."})

            if src_proto not in valid_protocols or dst_proto not in valid_protocols:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid protocol."})

            try:
                profile = self.server.state.add_sync_profile(profile_data)
                self.server.state.append_operation_log({
                    "time_utc": now_utc_iso(),
                    "event": "sync_profile_added",
                    "profile_id": profile.get("id"),
                    "profile_name": profile.get("name"),
                })
                return self._write_json(HTTPStatus.OK, profile)
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if parsed.path == "/api/sync/run":
            # POST: Run sync for a profile
            profile_id = str(data.get("profile_id", "")).strip()
            if not profile_id:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Profile ID required."})

            profiles = self.server.state.get_sync_profiles()
            profile = None
            for p in profiles:
                if p.get("id") == profile_id:
                    profile = p
                    break

            if not profile:
                return self._write_json(HTTPStatus.NOT_FOUND, {"error": "Profile not found."})

            try:
                result = sync_folders(profile, self.server.state)
                self.server.state.append_operation_log({
                    "time_utc": now_utc_iso(),
                    "event": "sync_executed",
                    "profile_id": profile_id,
                    "profile_name": profile.get("name"),
                    "result": result,
                })
                return self._write_json(HTTPStatus.OK, result)
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if parsed.path == "/api/sync/compare":
            profile_id = str(data.get("profile_id", "")).strip()
            if not profile_id:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Profile ID required."})
            profiles = self.server.state.get_sync_profiles()
            profile = next((p for p in profiles if p.get("id") == profile_id), None)
            if not profile:
                return self._write_json(HTTPStatus.NOT_FOUND, {"error": "Profile not found."})
            try:
                result = compare_folders(profile)
                return self._write_json(HTTPStatus.OK, result)
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if parsed.path == "/api/sync/dump-listing":
            profile_id = str(data.get("profile_id", "")).strip()
            if not profile_id:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Profile ID required."})
            profiles = self.server.state.get_sync_profiles()
            profile = next((p for p in profiles if p.get("id") == profile_id), None)
            if not profile:
                return self._write_json(HTTPStatus.NOT_FOUND, {"error": "Profile not found."})
            try:
                text = build_listing_text(profile)
                filename = f"listing_{profile_id[:8]}_{now_utc_iso()[:10]}.txt"
                return self._write_text_download(text, filename)
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if parsed.path == "/api/sync/update-profile":
            profile_id = str(data.get("profile_id", "")).strip()
            if not profile_id:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Profile ID required."})
            valid_protocols = {"local", "smb", "ftp"}
            src_proto = str(data.get("source_protocol", "local")).lower()
            dst_proto = str(data.get("dest_protocol", "local")).lower()
            if src_proto not in valid_protocols or dst_proto not in valid_protocols:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid protocol."})
            updates = {
                "name": str(data.get("name", "")).strip(),
                "source_protocol": src_proto,
                "dest_protocol": dst_proto,
                "source_host": str(data.get("source_host", "")).strip(),
                "dest_host": str(data.get("dest_host", "")).strip(),
                "source_path": str(data.get("source_path", "")).strip(),
                "dest_path": str(data.get("dest_path", "")).strip(),
                "source_user": str(data.get("source_user", "")).strip(),
                "dest_user": str(data.get("dest_user", "")).strip(),
                "recursive": bool(data.get("recursive", True)),
            }
            # Only update password if provided
            if data.get("source_pass"):
                updates["source_pass"] = str(data["source_pass"]).strip()
            if data.get("dest_pass"):
                updates["dest_pass"] = str(data["dest_pass"]).strip()
            if not updates["name"] or not updates["source_path"] or not updates["dest_path"]:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Missing required fields."})
            try:
                profile = self.server.state.update_sync_profile(profile_id, updates)
                self.server.state.append_operation_log({
                    "time_utc": now_utc_iso(), "event": "sync_profile_updated",
                    "profile_id": profile_id, "profile_name": profile.get("name"),
                })
                return self._write_json(HTTPStatus.OK, profile)
            except KeyError:
                return self._write_json(HTTPStatus.NOT_FOUND, {"error": "Profile not found."})
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if parsed.path == "/api/sync/browse":
            protocol = str(data.get("protocol", "local")).lower()
            path = str(data.get("path", "")).strip()

            if protocol == "local":
                try:
                    if not path:
                        dirs = [e.as_posix() for e in Path("/").iterdir()
                                if e.is_dir() and e.name.startswith("volume")]
                        dirs.sort()
                        return self._write_json(HTTPStatus.OK, {
                            "path": "/volume", "directories": dirs, "parent": None,
                        })
                    p = Path(path)
                    if not p.is_dir():
                        return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Chemin introuvable."})
                    sub_dirs = list_subdirectories(path)
                    parent_p = p.parent.as_posix()
                    return self._write_json(HTTPStatus.OK, {
                        "path": path,
                        "directories": sub_dirs,
                        "parent": None if parent_p == path else parent_p,
                    })
                except Exception as exc:
                    return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

            elif protocol == "ftp":
                host = str(data.get("host", "")).strip()
                user = str(data.get("user", "")).strip()
                password = str(data.get("password", "")).strip()
                if not host:
                    return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Hôte FTP requis."})
                try:
                    result = list_ftp_dirs(host, path, user, password)
                    return self._write_json(HTTPStatus.OK, result)
                except Exception as exc:
                    return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

            else:
                return self._write_json(HTTPStatus.BAD_REQUEST,
                    {"error": f"Navigation {protocol.upper()} non implémentée."})

        if parsed.path == "/api/sync/delete-profile":
            # POST: Delete a sync profile
            profile_id = str(data.get("profile_id", "")).strip()
            if not profile_id:
                return self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Profile ID required."})

            try:
                self.server.state.delete_sync_profile(profile_id)
                self.server.state.append_operation_log({
                    "time_utc": now_utc_iso(),
                    "event": "sync_profile_deleted",
                    "profile_id": profile_id,
                })
                return self._write_json(HTTPStatus.OK, {"ok": True})
            except Exception as exc:
                return self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args):
        # Keep stdout clean; operations are logged into the package log files.
        return


def parse_args():
    parser = argparse.ArgumentParser(description="Syno Batch Renamer server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str((Path(__file__).parent / "data").resolve()))
    return parser.parse_args()


def main():
    args = parse_args()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    state = AppState(data_dir)
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    server.state = state
    server.static_root = (Path(__file__).parent / "static").resolve()

    state.append_operation_log(
        {
            "time_utc": now_utc_iso(),
            "event": "service_started",
            "host": args.host,
            "port": args.port,
            "data_dir": data_dir.as_posix(),
        }
    )

    try:
        server.serve_forever()
    finally:
        state.append_operation_log(
            {
                "time_utc": now_utc_iso(),
                "event": "service_stopped",
            }
        )


if __name__ == "__main__":
    main()
