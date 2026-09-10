from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .config import AppConfig
from .discovery import DiscoveryError, MusicDiscovery
from .downloader import (
    DownloadError,
    DownloadManager,
    YtDlpDownloader,
    remove_collection,
)
from .services import create_services
from .spotify import SpotifyClient, SpotifyError, spotify_playlist_id
from .spotify_sync import SpotifySyncEngine
from .store import LibraryStore, ManifestError
from .storage import StorageError, safely_remove_player


_ARTWORK_HOSTS = {
    "i.ytimg.com",
    "lh3.googleusercontent.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
}
_ARTWORK_CONTENT_TYPES = {
    "image/apng",
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_MAX_ARTWORK_BYTES = 1024 * 1024
_MAX_ARTWORK_URL_CHARS = 2048
_MAX_QUERY_STRING_BYTES = 8 * 1024
_MAX_SEARCH_QUERY_CHARS = 200
_MAX_REQUEST_BYTES = 64 * 1024
_TRUSTED_HOSTS = ["127.0.0.1", "localhost"]
_SPOTIFY_SYNC_TERMINAL_STATUSES = {"complete", "partial", "failed"}


class _SpotifySyncBusy(SpotifyError):
    pass


class _SpotifySyncJobs:
    """Run one device-writing Spotify sync at a time and expose safe snapshots."""

    def __init__(self, engine: SpotifySyncEngine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    @property
    def active(self) -> bool:
        return self.active_job() is not None

    def active_job(self) -> dict[str, Any] | None:
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job["status"] not in _SPOTIFY_SYNC_TERMINAL_STATUSES:
                    return deepcopy(job)
        return None

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job else None

    def start(self, playlist_id: str) -> dict[str, Any]:
        with self._lock:
            if any(
                job["status"] not in _SPOTIFY_SYNC_TERMINAL_STATUSES
                for job in self._jobs.values()
            ):
                raise _SpotifySyncBusy(
                    "Wait for the current Spotify playlist sync to finish."
                )
            self._prune_locked()
            job_id = secrets.token_urlsafe(18)
            job = {
                "id": job_id,
                "playlist_id": playlist_id,
                "status": "queued",
                "progress": {
                    "phase": "queued",
                    "completed_total": 0,
                    "track_total": 0,
                    "available_total": 0,
                    "missing_total": 0,
                    "current_position": 0,
                    "current_title": "",
                },
                "report": None,
                "error": None,
            }
            self._jobs[job_id] = job
        worker = threading.Thread(
            target=self._run,
            args=(job_id, playlist_id),
            name=f"spotify-sync-{job_id[:8]}",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise SpotifyError("Could not start the Spotify playlist sync.") from exc
        snapshot = self.get(job_id)
        assert snapshot is not None
        return snapshot

    def _run(self, job_id: str, playlist_id: str) -> None:
        self._update(job_id, status="running")

        def report_progress(progress: dict[str, Any]) -> None:
            self._update(job_id, progress=dict(progress))

        try:
            report = self._engine.sync(playlist_id, progress=report_progress)
        except SpotifyError as exc:
            self._update(job_id, status="failed", error=str(exc))
        except Exception:
            self._update(
                job_id,
                status="failed",
                error="The Spotify playlist sync failed unexpectedly.",
            )
        else:
            self._update(
                job_id,
                status=str(report.get("status") or "complete"),
                report=report,
            )

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(values)

    def _prune_locked(self) -> None:
        terminal_ids = [
            job_id
            for job_id, job in self._jobs.items()
            if job["status"] in _SPOTIFY_SYNC_TERMINAL_STATUSES
        ]
        for job_id in terminal_ids[:-7]:
            self._jobs.pop(job_id, None)


def _validate_artwork_url(source_url: str) -> None:
    if not source_url or len(source_url) > _MAX_ARTWORK_URL_CHARS:
        raise ValueError("Unsupported artwork URL")
    parsed = urlparse(source_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Unsupported artwork URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ARTWORK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError("Unsupported artwork URL")


class _ArtworkRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_artwork_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@lru_cache(maxsize=32)
def _fetch_artwork(source_url: str) -> tuple[bytes, str]:
    _validate_artwork_url(source_url)
    artwork_request = Request(
        source_url,
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/png,image/jpeg,image/gif",
            "User-Agent": "Mozilla/5.0",
        },
    )
    opener = build_opener(_ArtworkRedirectHandler())
    with opener.open(artwork_request, timeout=10) as response:
        content_type = response.headers.get_content_type()
        if content_type not in _ARTWORK_CONTENT_TYPES:
            raise ValueError("Artwork response is not a supported raster image")
        artwork = response.read(_MAX_ARTWORK_BYTES + 1)
        if len(artwork) > _MAX_ARTWORK_BYTES:
            raise ValueError("Artwork response is too large")
    return artwork, content_type


def create_app(
    config: AppConfig | None = None,
    *,
    discovery: MusicDiscovery | None = None,
    manager: DownloadManager | None = None,
    store: LibraryStore | None = None,
    spotify: SpotifyClient | None = None,
    spotify_sync: SpotifySyncEngine | None = None,
    start_worker: bool = True,
) -> Flask:
    services = create_services(
        config,
        discovery=discovery,
        manager=manager,
        store=store,
        spotify=spotify,
        spotify_sync=spotify_sync,
        start_worker=start_worker,
    )
    config = services.config
    template_dir = Path(__file__).with_name("templates")
    static_dir = Path(__file__).with_name("static")
    app = Flask(
        __name__, template_folder=str(template_dir), static_folder=str(static_dir)
    )
    app.config.update(
        MAX_CONTENT_LENGTH=_MAX_REQUEST_BYTES,
        MAX_FORM_MEMORY_SIZE=_MAX_REQUEST_BYTES,
        MAX_FORM_PARTS=16,
        TRUSTED_HOSTS=_TRUSTED_HOSTS,
    )
    csrf_token = secrets.token_urlsafe(32)
    app.config["CSRF_TOKEN"] = csrf_token
    library_store = services.store
    music_discovery = services.discovery
    download_manager = services.downloads
    spotify_client = services.spotify
    sync_engine = services.spotify_sync
    spotify_sync_jobs = _SpotifySyncJobs(sync_engine) if sync_engine else None
    app.extensions["library_store"] = library_store
    app.extensions["music_discovery"] = music_discovery
    app.extensions["download_manager"] = download_manager
    app.extensions["spotify_client"] = spotify_client
    app.extensions["spotify_sync"] = sync_engine
    app.extensions["spotify_sync_jobs"] = spotify_sync_jobs
    storage_unavailable = threading.Event()

    def spotify_sync_active() -> bool:
        return bool(spotify_sync_jobs and spotify_sync_jobs.active) or bool(
            sync_engine and sync_engine.active
        )

    def player_storage_available() -> bool:
        if services.simulator:
            return services.simulator.connected and config.state_dir.is_dir()
        if config.player_volume is None:
            return not config.require_player_volume
        paths_available = (
            config.player_volume.is_dir() and config.state_dir.is_dir()
        )
        if not paths_available:
            return False
        if not storage_unavailable.is_set():
            return True
        if not os.path.ismount(config.player_volume):
            return False
        try:
            library_store.all()
        except (ManifestError, OSError):
            return False
        storage_unavailable.clear()
        return True

    @app.context_processor
    def security_context() -> dict[str, str]:
        return {"csrf_token": csrf_token}

    @app.before_request
    def protect_state_changes() -> None:
        if len(request.query_string) > _MAX_QUERY_STRING_BYTES:
            abort(414)
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        supplied_token = request.form.get("_csrf_token", "")
        if not supplied_token or not secrets.compare_digest(supplied_token, csrf_token):
            abort(403, description="This form has expired. Reload the page and try again.")

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'",
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    def page_context(**values: Any) -> dict[str, Any]:
        storage_available = player_storage_available()
        try:
            collections = (
                [
                    library_store.reconciled(item)
                    for item in reversed(library_store.all())
                ]
                if storage_available
                else []
            )
            jobs = download_manager.jobs() if storage_available else []
            spotify_syncs = (
                [sync_engine.reconciled(item) for item in sync_engine.syncs()]
                if sync_engine and storage_available
                else []
            )
        except (ManifestError, OSError):
            storage_unavailable.set()
            storage_available = False
            collections = []
            jobs = []
            spotify_syncs = []
        return {
            "collections": collections,
            "jobs": jobs,
            "spotify_syncs": spotify_syncs,
            "storage_busy": bool(jobs)
            or spotify_sync_active()
            or any(item.get("status") == "deleting" for item in collections),
            "library_dir": str(config.library_dir),
            "player_volume": str(config.player_volume) if config.player_volume else None,
            "storage_available": storage_available,
            "simulator": services.simulator is not None,
            **values,
        }

    def storage_unavailable_page() -> tuple[str, int]:
        return render_template(
            "index.html", **page_context(results=None, query="")
        ), 503

    @app.get("/")
    def index() -> str:
        return render_template("index.html", **page_context(results=None, query=""))

    def spotify_page_context(**values: Any) -> dict[str, Any]:
        configured = bool(spotify_client and spotify_client.configured)
        connected = bool(spotify_client and spotify_client.connected)
        storage_available = player_storage_available()
        sync_job = values.get("sync_job")
        sync_active = bool(
            sync_job
            and sync_job.get("status") not in _SPOTIFY_SYNC_TERMINAL_STATUSES
        )
        context: dict[str, Any] = {
            "configured": configured,
            "connected": connected,
            "redirect_uri": config.spotify_redirect_uri,
            "support_contact": config.spotify_support_contact,
            "profile": None,
            "playlists": [],
            "syncs": (
                sync_engine.syncs()
                if sync_engine and storage_available
                else []
            ),
            "storage_available": storage_available,
            "simulator": services.simulator is not None,
            "sync_job": sync_job,
            "sync_active": sync_active,
            **values,
        }
        if connected and spotify_client:
            try:
                context["profile"] = spotify_client.profile()
                if not sync_active:
                    context["playlists"] = spotify_client.playlists()
            except SpotifyError as exc:
                context.setdefault("error", str(exc))
        return context

    @app.get("/spotify")
    def spotify_integration() -> str:
        job_id = request.args.get("job", "").strip()
        sync_job = (
            spotify_sync_jobs.get(job_id)
            if spotify_sync_jobs and job_id
            else None
        )
        if sync_job is None and spotify_sync_jobs and not job_id:
            sync_job = spotify_sync_jobs.active_job()
        report = sync_job.get("report") if sync_job else None
        error = sync_job.get("error") if sync_job else None
        notice = None
        if request.args.get("connected") == "1":
            notice = "Spotify connected. Choose a playlist to sync."
        elif sync_job and sync_job["status"] == "partial":
            notice = "Playlist synced with missing tracks."
        elif sync_job and sync_job["status"] == "complete":
            notice = "Playlist sync completed."
        elif job_id and sync_job is None:
            error = "That Spotify sync job is no longer available."
        return render_template(
            "spotify.html",
            **spotify_page_context(
                notice=notice,
                error=error,
                report=report,
                sync_job=sync_job,
            ),
        )

    @app.post("/spotify/connect")
    def spotify_connect() -> tuple[str, int] | Any:
        if spotify_client is None:
            abort(404)
        try:
            return redirect(spotify_client.begin_authorization())
        except SpotifyError as exc:
            return render_template(
                "spotify.html",
                **spotify_page_context(error=str(exc), report=None),
            ), 400

    @app.get("/spotify/callback")
    def spotify_callback() -> tuple[str, int] | Any:
        if spotify_client is None:
            abort(404)
        authorization_error = request.args.get("error", "").strip()
        if authorization_error:
            return render_template(
                "spotify.html",
                **spotify_page_context(
                    error=f"Spotify connection was not completed: {authorization_error}",
                    report=None,
                ),
            ), 400
        try:
            spotify_client.finish_authorization(
                request.args.get("code", ""), request.args.get("state", "")
            )
        except SpotifyError as exc:
            return render_template(
                "spotify.html",
                **spotify_page_context(error=str(exc), report=None),
            ), 400
        return redirect(url_for("spotify_integration", connected="1"))

    @app.post("/spotify/disconnect")
    def spotify_disconnect() -> tuple[str, int] | Any:
        if spotify_client is None:
            abort(404)
        if spotify_sync_active():
            return render_template(
                "spotify.html",
                **spotify_page_context(
                    error=(
                        "Wait for the Spotify playlist sync to finish before "
                        "disconnecting."
                    ),
                    report=None,
                    sync_job=(
                        spotify_sync_jobs.active_job() if spotify_sync_jobs else None
                    ),
                ),
            ), 409
        spotify_client.disconnect()
        return redirect(url_for("spotify_integration"))

    @app.post("/spotify/sync")
    def spotify_sync_playlist() -> tuple[str, int] | Any:
        if sync_engine is None or spotify_sync_jobs is None:
            abort(404)
        if not player_storage_available():
            return render_template(
                "spotify.html",
                **spotify_page_context(
                    error="Connect the music storage before syncing a playlist.",
                    report=None,
                ),
            ), 503
        try:
            playlist_id = spotify_playlist_id(request.form.get("playlist_id", ""))
            sync_job = spotify_sync_jobs.start(playlist_id)
        except _SpotifySyncBusy as exc:
            return render_template(
                "spotify.html",
                **spotify_page_context(
                    error=str(exc),
                    report=None,
                    sync_job=spotify_sync_jobs.active_job(),
                ),
            ), 409
        except SpotifyError as exc:
            return render_template(
                "spotify.html",
                **spotify_page_context(error=str(exc), report=None),
            ), 400
        return redirect(
            url_for("spotify_integration", job=sync_job["id"]), code=303
        )

    @app.get("/api/spotify/syncs/<job_id>")
    def spotify_sync_status(job_id: str) -> Any:
        if spotify_sync_jobs is None:
            abort(404)
        sync_job = spotify_sync_jobs.get(job_id)
        if sync_job is None:
            abort(404)
        response = jsonify(
            {
                "id": sync_job["id"],
                "playlist_id": sync_job["playlist_id"],
                "status": sync_job["status"],
                "progress": sync_job["progress"],
                "error": sync_job["error"],
                "terminal": sync_job["status"]
                in _SPOTIFY_SYNC_TERMINAL_STATUSES,
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/search")
    def search() -> tuple[str, int] | str:
        query = request.args.get("q", "").strip()
        if not query:
            return render_template(
                "index.html",
                **page_context(results=[], query=query, error="Enter a search term."),
            ), 400
        if len(query) > _MAX_SEARCH_QUERY_CHARS:
            return render_template(
                "index.html",
                **page_context(
                    results=[],
                    query="",
                    error=f"Search terms are limited to {_MAX_SEARCH_QUERY_CHARS} characters.",
                ),
            ), 400
        try:
            results = music_discovery.search(query)
            return render_template(
                "index.html", **page_context(results=results, query=query)
            )
        except DiscoveryError as exc:
            return render_template(
                "index.html",
                **page_context(results=[], query=query, error=str(exc)),
            ), 502

    @app.get("/artwork")
    def artwork() -> Response:
        try:
            image, content_type = _fetch_artwork(request.args.get("url", ""))
        except (HTTPError, URLError, OSError, ValueError):
            abort(404)
        if content_type not in _ARTWORK_CONTENT_TYPES:
            abort(404)
        response = Response(image, content_type=content_type)
        response.headers["Cache-Control"] = "public, max-age=86400"
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response

    @app.post("/downloads")
    def create_download() -> tuple[str, int] | Any:
        if not player_storage_available():
            return storage_unavailable_page()
        source_url = request.form.get("url", "")
        kind = request.form.get("kind")
        kind_hint = kind if kind in {"album", "playlist"} else None
        try:
            collection = download_manager.enqueue(
                source_url,
                kind_hint,
                title_hint=request.form.get("title"),
                artist_hint=request.form.get("artist"),
            )
        except DownloadError as exc:
            return render_template(
                "index.html",
                **page_context(results=None, query="", error=str(exc)),
            ), 400
        return redirect(url_for("collection_detail", collection_id=collection["id"]))

    @app.get("/collections/<collection_id>")
    def collection_detail(collection_id: str) -> str | tuple[str, int]:
        if not player_storage_available():
            return storage_unavailable_page()
        collection = library_store.get(collection_id)
        if collection is None:
            abort(404)
        return render_template(
            "collection.html",
            collection=library_store.reconciled(collection),
            library_dir=str(config.library_dir),
        )

    @app.get("/collections/<collection_id>/remove")
    def confirm_remove(collection_id: str) -> str | tuple[str, int]:
        if not player_storage_available():
            return storage_unavailable_page()
        collection = library_store.get(collection_id)
        if collection is None:
            abort(404)
        return render_template("remove.html", collection=collection)

    @app.post("/collections/<collection_id>/retry")
    def retry_download(collection_id: str) -> tuple[str, int] | Any:
        if not player_storage_available():
            return storage_unavailable_page()
        try:
            download_manager.retry(collection_id)
        except KeyError:
            abort(404)
        except DownloadError as exc:
            collection = library_store.get(collection_id)
            if collection is None:
                abort(404)
            return render_template(
                "collection.html",
                collection=library_store.reconciled(collection),
                library_dir=str(config.library_dir),
                retry_error=str(exc),
            ), 409
        return redirect(url_for("collection_detail", collection_id=collection_id))

    @app.post("/collections/<collection_id>/remove")
    def perform_remove(collection_id: str) -> tuple[str, int] | Any:
        if not player_storage_available():
            return storage_unavailable_page()
        try:
            remove_collection(library_store, download_manager, collection_id)
        except KeyError:
            abort(404)
        except (DownloadError, ManifestError, OSError) as exc:
            collection = library_store.get(collection_id)
            return render_template(
                "remove.html", collection=collection, error=str(exc)
            ), 409
        return redirect(url_for("index"))

    @app.post("/storage/safely-remove")
    def safely_remove_storage() -> tuple[str, int] | str:
        if spotify_sync_active():
            return render_template(
                "index.html",
                **page_context(
                    results=None,
                    query="",
                    error="Wait for the Spotify playlist sync to finish before safely removing storage.",
                ),
            ), 409
        storage_unavailable.set()
        try:
            result = safely_remove_player(config, download_manager)
        except StorageError as exc:
            storage_unavailable.clear()
            return render_template(
                "index.html",
                **page_context(results=None, query="", error=str(exc)),
            ), 409
        return render_template(
            "safely_removed.html", volumes=result["volumes"],
            simulator=services.simulator is not None,
        )

    @app.post("/storage/simulator/<action>")
    def control_simulator(action: str) -> Any:
        if services.simulator is None or action not in {"connect", "disconnect"}:
            abort(404)
        if action == "disconnect" and spotify_sync_active():
            return render_template(
                "index.html",
                **page_context(
                    results=None,
                    query="",
                    error=(
                        "Wait for the Spotify playlist sync to finish before "
                        "disconnecting the virtual player."
                    ),
                ),
            ), 409
        try:
            if action == "connect":
                services.simulator.connect()
            else:
                services.simulator.disconnect(library_store)
        except (DownloadError, ManifestError, OSError, sqlite3.Error) as exc:
            return render_template(
                "index.html", **page_context(results=None, query="", error=str(exc))
            ), 409
        return redirect(url_for("index"))

    @app.get("/api/jobs")
    def api_jobs() -> Any:
        if not player_storage_available():
            return jsonify({"jobs": [], "storage_available": False}), 503
        jobs = []
        try:
            active_jobs = download_manager.jobs()
        except (ManifestError, OSError):
            storage_unavailable.set()
            return jsonify({"jobs": [], "storage_available": False}), 503
        for item in active_jobs:
            jobs.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "status": item["status"],
                    "progress": item.get("progress", {}),
                    "error": item.get("error"),
                    "detail_url": url_for(
                        "collection_detail", collection_id=item["id"]
                    ),
                }
            )
        return jsonify({"jobs": jobs})

    @app.get("/api/storage")
    def api_storage() -> Any:
        return jsonify({"storage_available": player_storage_available()})

    return app
