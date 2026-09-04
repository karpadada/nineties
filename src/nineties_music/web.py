from __future__ import annotations

import secrets
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
from .store import LibraryStore, ManifestError


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
    start_worker: bool = True,
) -> Flask:
    services = create_services(
        config,
        discovery=discovery,
        manager=manager,
        store=store,
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
    app.extensions["library_store"] = library_store
    app.extensions["music_discovery"] = music_discovery
    app.extensions["download_manager"] = download_manager

    @app.context_processor
    def security_context() -> dict[str, str]:
        return {"csrf_token": csrf_token}

    @app.before_request
    def protect_state_changes() -> None:
        if len(request.query_string) > _MAX_QUERY_STRING_BYTES:
            abort(414)
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        origin = request.headers.get("Origin")
        if origin is not None and origin != request.host_url.rstrip("/"):
            abort(403)
        supplied_token = request.form.get("_csrf_token", "")
        if not supplied_token or not secrets.compare_digest(supplied_token, csrf_token):
            abort(403)

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
        collections = [
            library_store.reconciled(item) for item in reversed(library_store.all())
        ]
        return {
            "collections": collections,
            "jobs": download_manager.jobs(),
            "library_dir": str(config.library_dir),
            "player_volume": str(config.player_volume) if config.player_volume else None,
            **values,
        }

    @app.get("/")
    def index() -> str:
        return render_template("index.html", **page_context(results=None, query=""))

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
    def collection_detail(collection_id: str) -> str:
        collection = library_store.get(collection_id)
        if collection is None:
            abort(404)
        return render_template(
            "collection.html",
            collection=library_store.reconciled(collection),
            library_dir=str(config.library_dir),
        )

    @app.get("/collections/<collection_id>/remove")
    def confirm_remove(collection_id: str) -> str:
        collection = library_store.get(collection_id)
        if collection is None:
            abort(404)
        return render_template("remove.html", collection=collection)

    @app.post("/collections/<collection_id>/retry")
    def retry_download(collection_id: str) -> tuple[str, int] | Any:
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

    @app.get("/api/jobs")
    def api_jobs() -> Any:
        jobs = []
        for item in download_manager.jobs():
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

    return app
