from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml
from flask import Flask, Response, jsonify, request, send_file

from .m3u import scan_m3u_groups


TERMINAL_STATES = {"complete", "failed", "cancelled"}


def create_app(work_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    state = AppState(work_dir or Path(os.environ.get("VSB_WORK_DIR", "/work")))

    @app.get("/")
    def index():
        return send_file(frontend_path())

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "work_dir": str(state.work_dir)})

    @app.post("/api/playlist/fetch")
    def fetch_playlist():
        payload = request.get_json(force=True) or {}
        try:
            url = payload.get("m3u_url") or build_m3u_url(payload)
            if not url:
                raise ValueError("Provider URL, username, and password are required.")
            groups = state.fetch_and_scan_playlist(url, str(payload.get("user_agent") or "vod-strm-builder/0.2"))
            return jsonify(group_response(groups, playlist_cached=True))
        except Exception as exc:
            return json_error(exc)

    @app.post("/api/playlist/text")
    def text_playlist():
        payload = request.get_json(force=True) or {}
        try:
            text = str(payload.get("text") or "")
            if not text.strip():
                raise ValueError("Playlist text is empty.")
            groups = state.write_and_scan_playlist(text)
            return jsonify(group_response(groups, playlist_cached=True))
        except Exception as exc:
            return json_error(exc)

    @app.post("/api/playlist/upload")
    def upload_playlist():
        try:
            upload = request.files.get("file")
            if upload is None:
                raise ValueError("No playlist file was uploaded.")
            upload.save(state.playlist_cache)
            groups = state.scan_cached_playlist()
            return jsonify(group_response(groups, playlist_cached=True))
        except Exception as exc:
            return json_error(exc)

    @app.post("/api/generate")
    def generate():
        payload = request.get_json(force=True) or {}
        try:
            job = state.start_job(payload)
            return jsonify(job.public())
        except Exception as exc:
            return json_error(exc)

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = state.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job.public())

    @app.get("/api/jobs/<job_id>/log")
    def job_log(job_id: str):
        job = state.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return Response(job.read_log(), mimetype="text/plain")

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        job = state.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        job.cancel()
        return jsonify(job.public())

    return app


class AppState:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.work_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.playlist_cache = self.work_dir / "playlist.m3u"
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def fetch_and_scan_playlist(self, url: str, user_agent: str) -> list[dict[str, object]]:
        headers = {"User-Agent": user_agent}
        try:
            with requests.get(url, stream=True, timeout=(20, 240), headers=headers) as response:
                response.raise_for_status()
                with self.playlist_cache.open("w", encoding="utf-8", errors="replace", newline="\n") as fh:

                    def lines():
                        for line in response.iter_lines(decode_unicode=True):
                            text = line or ""
                            fh.write(text + "\n")
                            yield text

                    return [group.to_dict() for group in scan_m3u_groups(lines())]
        except requests.RequestException as exc:
            raise RuntimeError(describe_playlist_fetch_error(exc)) from None

    def write_and_scan_playlist(self, text: str) -> list[dict[str, object]]:
        self.playlist_cache.write_text(text, encoding="utf-8")
        return self.scan_cached_playlist()

    def scan_cached_playlist(self) -> list[dict[str, object]]:
        with self.playlist_cache.open("r", encoding="utf-8", errors="replace") as fh:
            return [group.to_dict() for group in scan_m3u_groups(fh)]

    def start_job(self, payload: dict[str, Any]) -> "Job":
        settings = dict(payload.get("settings") or {})
        selected_groups = dict(payload.get("selected_groups") or {})
        validate_generate_settings(settings)

        job_id = uuid.uuid4().hex[:12]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        config_path = job_dir / "config.yml"
        groups_path = job_dir / "selected-groups.json"
        summary_path = job_dir / "last-run.json"
        log_path = job_dir / "generate.log"

        groups_path.write_text(json.dumps(selected_groups, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        env = job_environment(settings)
        config = build_config(settings, groups_path, self.playlist_cache if self.playlist_cache.exists() else None)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

        job = Job(
            job_id=job_id,
            job_dir=job_dir,
            config_path=config_path,
            summary_path=summary_path,
            log_path=log_path,
            env=env,
        )
        with self.lock:
            self.jobs[job_id] = job
        job.start()
        return job

    def get_job(self, job_id: str) -> "Job | None":
        with self.lock:
            return self.jobs.get(job_id)


class Job:
    def __init__(
        self,
        job_id: str,
        job_dir: Path,
        config_path: Path,
        summary_path: Path,
        log_path: Path,
        env: dict[str, str],
    ) -> None:
        self.id = job_id
        self.job_dir = job_dir
        self.config_path = config_path
        self.summary_path = summary_path
        self.log_path = log_path
        self.env = env
        self.status = "queued"
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.return_code: int | None = None
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        self.status = "running"
        self.started_at = time.time()
        command = [
            sys.executable,
            "-m",
            "vod_strm_builder.cli",
            "generate",
            "--config",
            str(self.config_path),
            "--summary-json",
            str(self.summary_path),
        ]
        with self.log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write("Running: vod-strm-builder generate --config config.yml --summary-json last-run.json\n\n")
            log.flush()
            self.process = subprocess.Popen(
                command,
                cwd=self.job_dir,
                env={**os.environ, **self.env},
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.return_code = self.process.wait()
        self.ended_at = time.time()
        if self.status == "cancelled":
            return
        self.status = "complete" if self.return_code == 0 else "failed"

    def cancel(self) -> None:
        if self.status in TERMINAL_STATES:
            return
        self.status = "cancelled"
        self.ended_at = time.time()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def read_log(self) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def read_summary(self) -> dict[str, Any] | None:
        if not self.summary_path.exists():
            return None
        try:
            return json.loads(self.summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "return_code": self.return_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "config_path": str(self.config_path),
            "summary_path": str(self.summary_path),
            "log_path": str(self.log_path),
            "summary": self.read_summary(),
        }


def frontend_path() -> Path:
    path = Path(__file__).resolve().parents[2] / "web" / "group-picker.html"
    if not path.exists():
        raise FileNotFoundError("Frontend file web/group-picker.html was not found.")
    return path


def group_response(groups: list[dict[str, object]], playlist_cached: bool) -> dict[str, object]:
    movie_total = sum(int(group["movie_count"]) for group in groups)
    series_total = sum(int(group["series_count"]) for group in groups)
    live_total = sum(int(group["live_count"]) for group in groups)
    return {
        "groups": groups,
        "playlist_cached": playlist_cached,
        "stats": {
            "groups": len(groups),
            "movie_entries": movie_total,
            "series_entries": series_total,
            "live_entries": live_total,
        },
    }


def build_config(settings: dict[str, Any], groups_path: Path, playlist_cache: Path | None) -> dict[str, Any]:
    provider: dict[str, Any] = {
        "server_url": clean_string(settings.get("server_url")).rstrip("/"),
        "username_env": "XTREAM_USERNAME",
        "password_env": "XTREAM_PASSWORD",
        "user_agent": clean_string(settings.get("user_agent")) or "vod-strm-builder/0.2",
    }
    if playlist_cache is not None:
        provider["m3u_file"] = str(playlist_cache)
    elif clean_string(settings.get("m3u_url")) or build_m3u_url(settings):
        provider["m3u_url_env"] = "XTREAM_M3U_URL"

    config: dict[str, Any] = {
        "provider": provider,
        "selected_groups_file": str(groups_path),
        "output": {
            "movies_dir": clean_string(settings.get("movies_dir")),
            "series_dir": clean_string(settings.get("series_dir")),
            "append_tmdb_id": as_bool(settings.get("append_tmdb")),
            "generate_nfo": as_bool(settings.get("generate_nfo")),
            "clean": as_bool(settings.get("clean_output")),
            "dry_run": as_bool(settings.get("dry_run")),
        },
        "filters": {
            "movie_groups": [],
            "series_groups": [],
            "movie_category_ids": [],
            "series_category_ids": [],
        },
        "series": {
            "source": "m3u",
            "require_selected_m3u_group": as_bool(settings.get("require_selected_group")),
            "quality_words": string_list(settings.get("quality_words")),
        },
        "tmdb": {
            "enabled": as_bool(settings.get("tmdb_enabled")),
            "api_key_env": "TMDB_API_KEY",
            "language": clean_string(settings.get("tmdb_language")) or "en-US",
            "cache_file": clean_string(settings.get("tmdb_cache")) or ".tmdb-cache.json",
            "lookup_missing_only": as_bool(settings.get("tmdb_missing_only")),
            "min_score": float(settings.get("tmdb_min_score") or 0.58),
            "fail_on_error": as_bool(settings.get("tmdb_fail_on_error")),
        },
        "jellyfin": {
            "enabled": as_bool(settings.get("jellyfin_enabled")),
            "api_key_env": "JELLYFIN_API_KEY",
            "scan_on_complete": as_bool(settings.get("jellyfin_scan")),
            "library_item_ids": string_list(settings.get("jellyfin_item_ids")),
        },
    }
    catalog_file = clean_string(settings.get("catalog_file"))
    if catalog_file:
        config["catalog_file"] = catalog_file
    jellyfin_url = clean_string(settings.get("jellyfin_url"))
    if jellyfin_url:
        config["jellyfin"]["server_url"] = jellyfin_url.rstrip("/")
    return config


def job_environment(settings: dict[str, Any]) -> dict[str, str]:
    env = {
        "XTREAM_USERNAME": clean_string(settings.get("username")),
        "XTREAM_PASSWORD": clean_string(settings.get("password")),
    }
    m3u_url = clean_string(settings.get("m3u_url")) or build_m3u_url(settings)
    if m3u_url:
        env["XTREAM_M3U_URL"] = m3u_url
    tmdb_key = clean_string(settings.get("tmdb_key"))
    if tmdb_key:
        env["TMDB_API_KEY"] = tmdb_key
    jellyfin_key = clean_string(settings.get("jellyfin_key"))
    if jellyfin_key:
        env["JELLYFIN_API_KEY"] = jellyfin_key
    return env


def validate_generate_settings(settings: dict[str, Any]) -> None:
    required = {
        "server_url": "Provider server URL",
        "username": "Provider username",
        "password": "Provider password",
        "movies_dir": "Movies directory",
        "series_dir": "TV directory",
    }
    missing = [label for key, label in required.items() if not clean_string(settings.get(key))]
    if missing:
        raise ValueError("Missing required settings: " + ", ".join(missing))
    if as_bool(settings.get("jellyfin_enabled")) and not clean_string(settings.get("jellyfin_key")):
        raise ValueError("Jellyfin is enabled but the Jellyfin API key is missing.")
    if as_bool(settings.get("jellyfin_enabled")) and not clean_string(settings.get("jellyfin_url")):
        raise ValueError("Jellyfin is enabled but the Jellyfin server URL is missing.")
    if as_bool(settings.get("tmdb_enabled")) and not clean_string(settings.get("tmdb_key")):
        raise ValueError("TMDB lookup is enabled but the TMDB API key is missing.")


def build_m3u_url(settings: dict[str, Any]) -> str:
    base = clean_string(settings.get("server_url")).rstrip("/")
    username = clean_string(settings.get("username"))
    password = clean_string(settings.get("password"))
    if not base or not username or not password:
        return ""
    return (
        f"{base}/get.php?"
        f"username={quote(username)}&password={quote(password)}"
        "&type=m3u_plus&output=ts"
    )


def string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def clean_string(value: Any) -> str:
    return str(value or "").strip()


def as_bool(value: Any) -> bool:
    return bool(value) and str(value).lower() not in {"false", "0", "no", "off"}


def json_error(exc: Exception):
    return jsonify({"error": str(exc)}), 400


def describe_playlist_fetch_error(exc: requests.RequestException) -> str:
    prefix = "Playlist fetch failed for the configured provider"
    if isinstance(exc, requests.Timeout):
        return f"{prefix}: request timed out."
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        reason = (exc.response.reason or "").strip()
        suffix = f"HTTP {status}"
        if reason:
            suffix = f"{suffix} {reason}"
        return f"{prefix}: {suffix}."
    if isinstance(exc, requests.ConnectionError):
        return f"{prefix}: connection failed."
    return f"{prefix}: {exc.__class__.__name__}."


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the VOD STRM Builder web app.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--work-dir", default=os.environ.get("VSB_WORK_DIR", "/work"))
    args = parser.parse_args()
    app = create_app(Path(args.work_dir))
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
