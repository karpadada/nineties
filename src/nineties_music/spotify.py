from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


SPOTIFY_SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
)
SPOTIFY_API_HOST = "api.spotify.com"
MAX_SPOTIFY_PAGES = 100


class SpotifyError(RuntimeError):
    pass


def spotify_playlist_id(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("spotify:playlist:"):
        candidate = candidate.rsplit(":", 1)[-1]
    elif "://" in candidate:
        parsed = urlparse(candidate)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"open.spotify.com", "www.open.spotify.com"}
        ):
            raise SpotifyError("Enter a Spotify playlist ID or URL.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] != "playlist":
            raise SpotifyError("Enter a Spotify playlist ID or URL.")
        candidate = parts[1]
    if not 10 <= len(candidate) <= 64 or not candidate.isalnum():
        raise SpotifyError("Enter a Spotify playlist ID or URL.")
    return candidate


class SpotifyClient:
    """Minimal Spotify Web API client using authorization-code PKCE."""

    def __init__(
        self,
        client_id: str | None,
        redirect_uri: str,
        credentials_dir: Path,
    ) -> None:
        self.redirect_uri = redirect_uri
        self.credentials_dir = credentials_dir.resolve()
        self._app_path = self.credentials_dir / "spotify-app.json"
        self._token_path = self.credentials_dir / "spotify-token.json"
        self._pending_path = self.credentials_dir / "spotify-oauth.json"
        supplied_client_id = (client_id or "").strip()
        stored_app = self._read_json(self._app_path) or {}
        self.client_id = supplied_client_id or str(
            stored_app.get("client_id") or ""
        ).strip()
        if supplied_client_id and supplied_client_id != stored_app.get("client_id"):
            if stored_app.get("client_id"):
                self._token_path.unlink(missing_ok=True)
            self._write_json(self._app_path, {"client_id": supplied_client_id})

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    @property
    def connected(self) -> bool:
        token = self._read_json(self._token_path)
        return bool(token and (token.get("access_token") or token.get("refresh_token")))

    def begin_authorization(self) -> str:
        if not self.configured:
            raise SpotifyError(
                "Spotify is not configured. Set MUSIC_SPOTIFY_CLIENT_ID first."
            )
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        self._write_json(
            self._pending_path,
            {
                "state": state,
                "verifier": verifier,
                "expires_at": int(time.time()) + 600,
            },
        )
        return "https://accounts.spotify.com/authorize?" + urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(SPOTIFY_SCOPES),
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )

    def finish_authorization(self, code: str, state: str) -> None:
        pending = self._read_json(self._pending_path)
        if (
            not pending
            or not state
            or not secrets.compare_digest(str(pending.get("state") or ""), state)
            or int(pending.get("expires_at") or 0) < int(time.time())
        ):
            raise SpotifyError("The Spotify connection request expired. Start again.")
        verifier = str(pending.get("verifier") or "")
        self._pending_path.unlink(missing_ok=True)
        if not code or not verifier:
            raise SpotifyError("Spotify did not return a usable authorization code.")
        token = self._token_request(
            {
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            }
        )
        self._save_token(token)

    def disconnect(self) -> None:
        self._token_path.unlink(missing_ok=True)
        self._pending_path.unlink(missing_ok=True)

    def profile(self) -> dict[str, str]:
        payload = self._api_get("/v1/me")
        images = payload.get("images") or []
        image_url = ""
        if isinstance(images, list) and images and isinstance(images[0], dict):
            image_url = str(images[0].get("url") or "")
        return {
            "id": str(payload.get("id") or ""),
            "display_name": str(payload.get("display_name") or "Spotify user"),
            "url": str((payload.get("external_urls") or {}).get("spotify") or ""),
            "image_url": image_url,
        }

    def playlists(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for item in self._paged("/v1/me/playlists?limit=50"):
            playlist_id = str(item.get("id") or "")
            if not playlist_id:
                continue
            owner = item.get("owner") or {}
            playlist_items = item.get("items") or item.get("tracks") or {}
            values.append(
                {
                    "id": playlist_id,
                    "name": str(item.get("name") or "Untitled playlist"),
                    "owner": str(
                        owner.get("display_name") or owner.get("id") or "Unknown owner"
                    ),
                    "track_total": int(playlist_items.get("total") or 0),
                    "url": str(
                        (item.get("external_urls") or {}).get("spotify") or ""
                    ),
                }
            )
        return values

    def playlist(self, raw_playlist_id: str) -> dict[str, Any]:
        playlist_id = spotify_playlist_id(raw_playlist_id)
        payload = self._api_get(
            "/v1/playlists/"
            + quote(playlist_id, safe="")
            + "?fields=id,name,owner(id,display_name),external_urls,snapshot_id"
        )
        owner = payload.get("owner") or {}
        return {
            "id": playlist_id,
            "name": str(payload.get("name") or "Untitled playlist"),
            "owner": str(
                owner.get("display_name") or owner.get("id") or "Unknown owner"
            ),
            "snapshot_id": str(payload.get("snapshot_id") or ""),
            "url": str((payload.get("external_urls") or {}).get("spotify") or ""),
            "tracks": list(
                self._playlist_tracks(
                    f"/v1/playlists/{quote(playlist_id, safe='')}/items?limit=50"
                )
            ),
        }

    def _playlist_tracks(self, path: str):
        position = 0
        for entry in self._paged(path):
            position += 1
            item = entry.get("item") or entry.get("track") or {}
            if not isinstance(item, dict):
                item = {}
            artists = item.get("artists") or []
            artist_names = [
                str(artist.get("name") or "").strip()
                for artist in artists
                if isinstance(artist, dict) and artist.get("name")
            ]
            album = item.get("album") or {}
            yield {
                "position": position,
                "id": str(item.get("id") or ""),
                "uri": str(item.get("uri") or ""),
                "type": str(item.get("type") or "track"),
                "name": str(item.get("name") or "Unavailable track"),
                "artists": artist_names,
                "album": str(album.get("name") or ""),
                "duration_seconds": round(int(item.get("duration_ms") or 0) / 1000),
                "is_local": bool(entry.get("is_local") or item.get("is_local")),
                "available": bool(item and item.get("id")),
                "spotify_url": str(
                    (item.get("external_urls") or {}).get("spotify") or ""
                ),
            }

    def _paged(self, path: str):
        next_url: str | None = path
        pages = 0
        while next_url:
            pages += 1
            if pages > MAX_SPOTIFY_PAGES:
                raise SpotifyError("Spotify returned too many result pages.")
            payload = self._api_get(next_url)
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise SpotifyError("Spotify returned an invalid list response.")
            for item in items:
                if isinstance(item, dict):
                    yield item
            next_value = payload.get("next")
            next_url = str(next_value) if next_value else None

    def _api_get(self, path_or_url: str) -> dict[str, Any]:
        token = self._valid_access_token()
        url = (
            path_or_url
            if path_or_url.startswith("https://")
            else "https://api.spotify.com" + path_or_url
        )
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != SPOTIFY_API_HOST:
            raise SpotifyError("Spotify returned an unsafe pagination URL.")
        try:
            return self._request_json(
                Request(url, headers={"Authorization": f"Bearer {token}"})
            )
        except SpotifyError as exc:
            if "HTTP 401" not in str(exc):
                raise
        token = self._refresh_access_token()
        return self._request_json(
            Request(url, headers={"Authorization": f"Bearer {token}"})
        )

    def _valid_access_token(self) -> str:
        token = self._read_json(self._token_path)
        if not token:
            raise SpotifyError("Connect Spotify before using playlist sync.")
        access_token = str(token.get("access_token") or "")
        if access_token and int(token.get("expires_at") or 0) > int(time.time()) + 60:
            return access_token
        return self._refresh_access_token(token)

    def _refresh_access_token(self, current: dict[str, Any] | None = None) -> str:
        current = current or self._read_json(self._token_path)
        refresh_token = str((current or {}).get("refresh_token") or "")
        if not refresh_token:
            raise SpotifyError("The Spotify connection expired. Connect it again.")
        token = self._token_request(
            {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        if not token.get("refresh_token"):
            token["refresh_token"] = refresh_token
        self._save_token(token)
        return str(token.get("access_token") or "")

    def _token_request(self, fields: dict[str, str]) -> dict[str, Any]:
        request = Request(
            "https://accounts.spotify.com/api/token",
            data=urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._request_json(request)

    def _request_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                error_detail = detail.get("error")
                error_message = (
                    error_detail.get("message")
                    if isinstance(error_detail, dict)
                    else error_detail
                )
                message = detail.get("error_description") or error_message
            except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                message = None
            raise SpotifyError(
                f"Spotify HTTP {exc.code}: {message or exc.reason}"
            ) from exc
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise SpotifyError(f"Could not reach Spotify: {exc}") from exc
        if not isinstance(payload, dict):
            raise SpotifyError("Spotify returned an invalid response.")
        return payload

    def _save_token(self, token: dict[str, Any]) -> None:
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise SpotifyError("Spotify did not return an access token.")
        stored = dict(token)
        stored["expires_at"] = int(time.time()) + int(token.get("expires_in") or 3600)
        self._write_json(self._token_path, stored)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
