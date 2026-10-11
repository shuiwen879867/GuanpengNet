"""A file-locked match store usable by independent web and bot processes."""

import json
import os
import tempfile
from pathlib import Path

import aiohttp
from filelock import FileLock, Timeout
from guanpeng_runtime import API_BASE, API_KEYS, data_dir

MAX_PER_RUN = 500
MATCHES_PER_PLAYER = 40


async def rate_limited_fetch(session, endpoint, params, is_list=False):
    for key in API_KEYS:
        try:
            async with session.get(
                f"{API_BASE}{endpoint}",
                params={**params, "key": key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 429:
                    continue
                response.raise_for_status()
                return await response.json()
        except (TimeoutError, aiohttp.ClientError, ValueError):
            continue
    return None


class MatchDatabase:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._db_path = self.db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._file_lock = FileLock(self.db_path + ".lock", timeout=30)
        with self._file_lock:
            if not Path(self.db_path).exists():
                self._write(self._read())

    def _read(self):
        path = Path(self.db_path)
        if not path.exists():
            return {"matches": {}, "pending": [], "meta": {"total": 0}}
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("matches", {})
        data.setdefault("pending", [])
        data.setdefault("meta", {})
        return data

    def _write(self, data):
        data["meta"]["total"] = len(data["matches"])
        descriptor, filename = tempfile.mkstemp(
            dir=Path(self.db_path).parent, suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(filename, self.db_path)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)

    def has_match(self, match_id):
        return self.get_match(match_id) is not None

    def get_match(self, match_id):
        with self._file_lock:
            return self._read()["matches"].get(match_id)

    @property
    def total_matches(self):
        with self._file_lock:
            return len(self._read()["matches"])

    @property
    def pending_count(self):
        with self._file_lock:
            return len(self._read()["pending"])

    def add_pending(self, match_ids):
        with self._file_lock:
            data = self._read()
            existing = set(data["matches"]) | set(data["pending"])
            for match_id in match_ids:
                if match_id and match_id not in existing:
                    data["pending"].append(match_id)
                    existing.add(match_id)
            self._write(data)

    def collect_from_player_matches(self, matches):
        self.add_pending([m.get("id") for m in matches[:MATCHES_PER_PLAYER]])

    async def _fetch_match(self, session, match_id):
        result = await rate_limited_fetch(session, "/match/megawalls", {"id": match_id})
        if result and result.get("status") == 200:
            return result.get("data")
        return None

    async def process_pending(self, session, max_per_run=MAX_PER_RUN):
        if not API_KEYS:
            return 0
        processor_lock = FileLock(self.db_path + ".processor.lock", timeout=0)
        try:
            processor_lock.acquire()
        except Timeout:
            return 0
        success = 0
        try:
            with self._file_lock:
                pending = self._read()["pending"][:max_per_run]
            for match_id in pending:
                match = await self._fetch_match(session, match_id)
                if not match:
                    continue
                with self._file_lock:
                    data = self._read()
                    data["matches"][match_id] = match
                    data["pending"] = [
                        item for item in data["pending"] if item != match_id
                    ]
                    self._write(data)
                success += 1
            return success
        finally:
            processor_lock.release()


_instances = {}


def get_shared_db(service="web"):
    path = str(data_dir(service) / "match_db.json")
    if path not in _instances:
        _instances[path] = MatchDatabase(path)
    return _instances[path]
