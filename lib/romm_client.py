import os
import json
import base64
import ssl
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING

import decky

if TYPE_CHECKING:
    from typing import Protocol

    class _RommClientDeps(Protocol):
        settings: dict


class RommClientMixin:
    def _load_platform_map(self):
        config_path = os.path.join(decky.DECKY_PLUGIN_DIR, "defaults", "config.json")
        with open(config_path, "r") as f:
            config = json.load(f)
        return config.get("platform_map", {})

    def _resolve_system(self, platform_slug, platform_fs_slug=None):
        platform_map = self._load_platform_map()
        if platform_slug in platform_map:
            return platform_map[platform_slug]
        if platform_fs_slug and platform_fs_slug in platform_map:
            return platform_map[platform_fs_slug]
        return platform_slug

    def _romm_request(self, path):
        url = self.settings["romm_url"].rstrip("/") + path
        req = urllib.request.Request(url, method="GET")
        credentials = base64.b64encode(
            f"{self.settings['romm_user']}:{self.settings['romm_pass']}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {credentials}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def _romm_download(self, path, dest, progress_callback=None):
        # URL-encode the path, preserving already-valid characters (/:?=&)
        # RomM API returns paths with unencoded spaces in query params
        encoded_path = urllib.parse.quote(path, safe="/:?=&@")
        url = self.settings["romm_url"].rstrip("/") + encoded_path
        req = urllib.request.Request(url, method="GET")
        credentials = base64.b64encode(
            f"{self.settings['romm_user']}:{self.settings['romm_pass']}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {credentials}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else 0
            downloaded = 0
            block_size = 8192
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        progress_callback(downloaded, total)
        if total > 0 and downloaded != total:
            raise IOError(f"Download incomplete: got {downloaded} bytes, expected {total}")
