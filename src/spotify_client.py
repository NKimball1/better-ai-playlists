"""Thin Spotify Web API client: auth header, pagination, 429 retry."""
import time

import requests

from .spotify_auth import get_access_token

BASE = "https://api.spotify.com/v1"


class SpotifyClient:
    def __init__(self):
        self._token = get_access_token()

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET with 429 retry. `path` is relative (e.g. '/me/tracks') or a full next-page URL."""
        url = path if path.startswith("http") else BASE + path
        for attempt in range(5):
            resp = requests.get(url, headers=self._headers(), params=params)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2")) + 1
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                # token expired mid-run; refresh once
                self._token = get_access_token()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"GET {url} failed after retries")

    def post(self, path: str, body: dict) -> dict:
        url = path if path.startswith("http") else BASE + path
        for attempt in range(5):
            resp = requests.post(url, headers=self._headers(), json=body)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2")) + 1
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                self._token = get_access_token()
                continue
            resp.raise_for_status()
            return resp.json() if resp.text else {}
        raise RuntimeError(f"POST {url} failed after retries")

    def paginate(self, path: str, params: dict | None = None, max_items: int | None = None):
        """Yield items from a paginated endpoint, following `next` links."""
        page = self.get(path, params)
        count = 0
        while True:
            for item in page.get("items", []):
                yield item
                count += 1
                if max_items and count >= max_items:
                    return
            nxt = page.get("next")
            if not nxt:
                return
            page = self.get(nxt)
