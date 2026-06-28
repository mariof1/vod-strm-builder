from __future__ import annotations

from typing import Any

import requests

from .models import JellyfinConfig


class JellyfinClient:
    def __init__(self, config: JellyfinConfig, timeout: int = 30) -> None:
        if not config.server_url:
            raise ValueError("Jellyfin is enabled but jellyfin.server_url is missing.")
        if not config.api_key:
            raise ValueError(f"Jellyfin is enabled but no API key was provided via {config.api_key_env}.")
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Emby-Token": config.api_key})

    def refresh_all_libraries(self) -> None:
        response = self.session.post(
            f"{self.config.server_url}/Library/Refresh",
            timeout=self.timeout,
        )
        response.raise_for_status()

    def refresh_items(self, item_ids: tuple[str, ...]) -> int:
        count = 0
        for item_id in item_ids:
            response = self.session.post(
                f"{self.config.server_url}/Items/{item_id}/Refresh",
                params={
                    "Recursive": "true",
                    "MetadataRefreshMode": "Default",
                    "ImageRefreshMode": "Default",
                    "ReplaceAllMetadata": "false",
                    "ReplaceAllImages": "false",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            count += 1
        return count


def notify_jellyfin(config: JellyfinConfig, dry_run: bool) -> dict[str, Any]:
    if not config.enabled:
        return {"jellyfin_enabled": 0}
    if dry_run:
        return {"jellyfin_enabled": 1, "jellyfin_skipped": "dry_run"}
    client = JellyfinClient(config)
    refreshed_items = 0
    if config.library_item_ids:
        refreshed_items = client.refresh_items(config.library_item_ids)
    elif config.scan_on_complete:
        client.refresh_all_libraries()
    return {
        "jellyfin_enabled": 1,
        "jellyfin_library_scan_requested": int(config.scan_on_complete and not config.library_item_ids),
        "jellyfin_items_refreshed": refreshed_items,
    }

