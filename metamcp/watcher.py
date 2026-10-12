"""Hot-reload servers.json via mtime polling — diff & apply changes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from .logger import log

if TYPE_CHECKING:
    from .backend import BackendProxy

_watcher_task: asyncio.Task | None = None


async def _reload_config(
    config_path: Path,
    backends: dict[str, BackendProxy],
) -> None:
    """Diff current backends against new config and apply changes."""
    from .config import load_config, ServerConfig
    from .backend import BackendProxy as BP

    try:
        new_config = load_config(config_path)
    except Exception as e:
        log.error(f"[hot-reload] Invalid config, ignoring: {e}")
        return

    old_names = set(backends.keys())
    new_names = set(new_config.servers.keys())

    # Stop removed backends
    for name in old_names - new_names:
        log.info(f"[hot-reload] Removing backend: {name}")
        bp = backends.pop(name)
        await bp.stop()

    # Start new backends
    for name in new_names - old_names:
        log.info(f"[hot-reload] Adding backend: {name}")
        sc = new_config.servers[name]
        bp = BP(name, sc)
        backends[name] = bp
        if sc.lazy:
            await bp.start_lazy()
        else:
            try:
                await bp.start()
            except Exception:
                log.error(f"[hot-reload] [{name}] Failed to start")

    # Restart modified backends (config changed)
    for name in new_names & old_names:
        old_bp = backends[name]
        new_sc = new_config.servers[name]
        # Compare serialized configs for simplicity
        if _config_changed(old_bp.current_config, new_sc):
            log.info(f"[hot-reload] Config changed for: {name}, restarting...")
            await old_bp.stop()
            bp = BP(name, new_sc)
            backends[name] = bp
            if new_sc.lazy:
                await bp.start_lazy()
            else:
                try:
                    await bp.start()
                except Exception:
                    log.error(f"[hot-reload] [{name}] Failed to restart")

    # Update global config reference
    from . import app as app_module
    app_module.config = new_config

    log.info(f"[hot-reload] Reload complete ({len(backends)} backends)")


def _config_changed(old: object, new: object) -> bool:
    """Compare two ServerConfig instances by serializing to JSON."""
    import dataclasses

    def _to_dict(obj: object) -> dict:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        return {}

    return json.dumps(_to_dict(old), sort_keys=True) != json.dumps(
        _to_dict(new), sort_keys=True
    )


async def _watch_loop(
    config_path: Path,
    backends: dict[str, BackendProxy],
) -> None:
    """Watch servers.json for changes via mtime polling.

    Uses simple stat() polling instead of watchfiles to avoid spawning
    background processes (find.exe) on Windows that cause IO storms.
    """
    log.info(f"[hot-reload] Watching {config_path} (mtime polling, 2s interval)")
    last_mtime: float = 0.0
    try:
        if config_path.exists():
            last_mtime = config_path.stat().st_mtime
    except OSError:
        pass

    try:
        while True:
            await asyncio.sleep(2)
            try:
                if not config_path.exists():
                    continue
                current_mtime = config_path.stat().st_mtime
                if current_mtime != last_mtime and last_mtime != 0.0:
                    log.info("[hot-reload] servers.json changed, reloading...")
                    try:
                        await _reload_config(config_path, backends)
                    except Exception as e:
                        log.error(f"[hot-reload] Error during reload: {e}")
                last_mtime = current_mtime
            except OSError:
                pass
    except asyncio.CancelledError:
        pass


def start_watcher(
    config_path: Path,
    backends: dict[str, BackendProxy],
) -> None:
    """Start the hot-reload watcher as a background task."""
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        return
    _watcher_task = asyncio.create_task(_watch_loop(config_path, backends))


def stop_watcher() -> None:
    """Cancel the hot-reload watcher."""
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        _watcher_task = None
