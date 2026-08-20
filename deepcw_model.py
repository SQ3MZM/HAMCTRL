"""
deepcw_model.py — server-side management of the DeepCW model

- Downloads the ONNX model from GitHub (e04/web-deep-cw-decoder)
- Serves it locally via /api/deepcw/model
- Checks for updates (commit SHA from the GitHub API)
- Caches it in a local file, deepcw/model_balanced.onnx
"""
import asyncio
import hashlib
import json
import os
import pathlib
import time

import aiohttp

# Data directory (APPDATA) — the model survives product updates.
try:
    from config import DATA as _DATA
    MODEL_DIR = pathlib.Path(_DATA) / "deepcw"
except Exception:
    MODEL_DIR = pathlib.Path("deepcw")
MODEL_FILE  = MODEL_DIR / "model_balanced.onnx"
META_FILE   = MODEL_DIR / "meta.json"

GITHUB_API  = "https://api.github.com/repos/e04/web-deep-cw-decoder/commits?path=public/model_balanced.onnx&per_page=1"
MODEL_URL   = "https://cdn.jsdelivr.net/gh/e04/web-deep-cw-decoder@main/public/model_balanced.onnx"

CHECK_INTERVAL = 6 * 3600   # check every 6h


class DeepCWModelManager:
    def __init__(self):
        self.model_bytes: bytes | None = None
        self.meta: dict = {}
        self._lock = asyncio.Lock()

    def _load_meta(self) -> dict:
        try:
            return json.loads(META_FILE.read_text())
        except Exception:
            return {}

    def _save_meta(self, meta: dict):
        MODEL_DIR.mkdir(exist_ok=True)
        META_FILE.write_text(json.dumps(meta, indent=2))

    def get_status(self) -> dict:
        m = self._load_meta()
        has_model = MODEL_FILE.exists()
        return {
            "hasModel":    has_model,
            "sizeBytes":   MODEL_FILE.stat().st_size if has_model else 0,
            "sha":         m.get("sha", ""),
            "downloadedAt": m.get("downloaded_at", ""),
            "checkedAt":   m.get("checked_at", ""),
            "latestSha":   m.get("latest_sha", ""),
            "updateAvailable": (
                m.get("latest_sha") and
                m.get("sha") and
                m.get("latest_sha") != m.get("sha")
            ),
        }

    async def check_update(self) -> dict:
        """Check the latest commit SHA from the GitHub API."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    GITHUB_API,
                    headers={"Accept": "application/vnd.github.v3+json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        return {"ok": False, "error": f"GitHub API HTTP {r.status}"}
                    data = await r.json()
                    if not data:
                        return {"ok": False, "error": "No commits found"}
                    latest_sha = data[0]["sha"][:12]

            meta = self._load_meta()
            meta["latest_sha"]  = latest_sha
            meta["checked_at"]  = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            self._save_meta(meta)

            current = meta.get("sha", "")
            update_available = bool(current and current != latest_sha)
            return {
                "ok": True,
                "latestSha": latest_sha,
                "currentSha": current,
                "updateAvailable": update_available,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def download_model(self, broadcast_fn=None) -> dict:
        """Download the model from the CDN and save it locally."""
        async with self._lock:
            try:
                save_path = str(MODEL_FILE.resolve())
                MODEL_DIR.mkdir(exist_ok=True)

                async def _bcast(msg, pct, received=0, total=0):
                    if not broadcast_fn: return
                    detail = f"{received/1e6:.1f} / {total/1e6:.1f} MB" if total else f"{received/1e6:.1f} MB"
                    await broadcast_fn({
                        "type": "deepcw_progress",
                        "msg":  msg,
                        "pct":  pct,
                        "detail": detail,
                        "savePath": save_path,
                    })

                await _bcast("Łączenie z CDN...", 0)

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        MODEL_URL,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as r:
                        if r.status != 200:
                            return {"ok": False, "error": f"HTTP {r.status}"}
                        total = int(r.headers.get("Content-Length", 0))
                        data  = bytearray()
                        async for chunk in r.content.iter_chunked(65536):
                            data.extend(chunk)
                            pct = int(len(data) / total * 100) if total else 0
                            if len(data) % (256*1024) < 65536:  # roughly every 256KB
                                await _bcast(f"Pobieranie modelu DeepCW...", pct, len(data), total)

                model_bytes = bytes(data)
                MODEL_FILE.write_bytes(model_bytes)
                # We do NOT keep a copy in RAM — the engine (deepcw_engine)
                # reads the file from disk via ONNX Runtime. An in-memory
                # copy would just be 15 MB wasted.

                sha = hashlib.sha256(model_bytes).hexdigest()[:12]
                meta = self._load_meta()
                meta["sha"]           = meta.get("latest_sha", sha)
                meta["sha256"]        = sha
                meta["downloaded_at"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
                meta["size_bytes"]    = len(model_bytes)
                self._save_meta(meta)

                await _bcast(f"✓ Model gotowy ({len(model_bytes)/1e6:.1f} MB) → {save_path}", 100, len(model_bytes), len(model_bytes))
                print(f"[deepcw] Model downloaded: {len(model_bytes)/1e6:.1f} MB -> {save_path}", flush=True)
                return {"ok": True, "sizeBytes": len(model_bytes), "sha": sha, "savePath": save_path}
            except Exception as e:
                print(f"[deepcw] Download error: {e}", flush=True)
                if broadcast_fn:
                    await broadcast_fn({"type": "deepcw_progress", "msg": f"✗ Błąd: {e}", "pct": -1})
                return {"ok": False, "error": str(e)}

    def load_from_disk(self):
        """Check whether the model is present (without loading it into RAM).

        This used to load the whole file into memory — 15 MB held for no
        reason, since inference is done by deepcw_engine reading the file directly."""
        if MODEL_FILE.exists():
            _mb = MODEL_FILE.stat().st_size / 1e6
            print(f"[deepcw] Model on disk: {_mb:.1f} MB ({MODEL_FILE})", flush=True)

    async def auto_check_loop(self, broadcast_fn=None):
        """Update-check loop, every 6h."""
        await asyncio.sleep(30)  # startup delay
        while True:
            try:
                result = await self.check_update()
                if result.get("updateAvailable") and broadcast_fn:
                    await broadcast_fn({
                        "type": "deepcw_update",
                        "msg":  f"Dostępna aktualizacja modelu DeepCW (SHA: {result['latestSha']})",
                        "latestSha": result["latestSha"],
                    })
                    print(f"[deepcw] Update: {result['latestSha']}", flush=True)
            except Exception as e:
                print(f"[deepcw] check_update error: {e}", flush=True)
            await asyncio.sleep(CHECK_INTERVAL)


# Singleton
deepcw_manager = DeepCWModelManager()
