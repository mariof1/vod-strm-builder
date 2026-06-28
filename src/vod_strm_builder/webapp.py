from __future__ import annotations

import argparse
from collections import defaultdict
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
import yaml
from flask import Flask, Response, jsonify, request, send_file

from .m3u import decode_m3u_line, scan_m3u_groups
from .models import DEFAULT_USER_AGENT, MovieItem, ProviderConfig, SeriesItem
from .xtream import XtreamClient


TERMINAL_STATES = {"complete", "failed", "cancelled"}
logging.basicConfig(
    level=os.environ.get("VSB_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger(__name__)


def create_app(work_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    state = AppState(work_dir or Path(os.environ.get("VSB_WORK_DIR", "/work")))

    @app.get("/")
    def index():
        return send_file(frontend_path())

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "work_dir": str(state.work_dir)})

    @app.get("/api/settings")
    def get_settings():
        return jsonify(state.load_settings())

    @app.get("/api/groups")
    def get_groups():
        return jsonify(state.load_or_rebuild_group_cache())

    @app.post("/api/settings")
    def save_settings():
        try:
            payload = api_payload()
            state.save_settings(payload)
            return jsonify({"ok": True, "settings": payload})
        except Exception as exc:
            LOG.exception("settings save failed")
            return json_error(exc)

    @app.post("/api/playlist/fetch")
    def fetch_playlist():
        try:
            payload = api_payload()
            providers = settings_providers(payload)
            if not providers:
                raise ValueError("Provider URL, username, and password are required.")

            all_groups: list[dict[str, object]] = []
            playlist_cached = True
            sources: set[str] = set()
            warnings: list[str] = []
            for provider in providers:
                provider_id = provider_identifier(provider)
                provider_name = provider_label(provider)
                url = clean_string(provider.get("m3u_url")) or build_m3u_url(provider)
                if not url:
                    raise ValueError(f"Provider {provider_name} is missing an M3U URL or URL credentials.")
                LOG.info("fetching playlist groups provider=%s url=%s", provider_name, redact_url(url))
                try:
                    groups = state.fetch_and_scan_playlist(
                        url,
                        str(provider.get("user_agent") or DEFAULT_USER_AGENT),
                        provider_id,
                    )
                    source = "m3u"
                except Exception as playlist_exc:
                    LOG.warning("m3u fetch failed provider=%s error=%s", provider_name, playlist_exc)
                    cached_groups = state.scan_cached_playlist_if_available(provider_id)
                    if cached_groups is not None:
                        groups = cached_groups
                        source = "m3u"
                        warnings.append(f"{provider_name}: live playlist fetch failed; using cached playlist. {playlist_exc}")
                    else:
                        try:
                            groups = state.fetch_xtream_groups(provider)
                            playlist_cached = False
                            source = "xtream_api"
                            warnings.append(f"{provider_name}: {playlist_exc}")
                        except Exception:
                            raise playlist_exc
                groups = tag_groups(groups, provider_id, provider_name, source)
                all_groups.extend(groups)
                sources.add(source)
                LOG.info("provider groups fetched provider=%s source=%s groups=%s", provider_name, source, len(groups))

            source = "mixed" if len(sources) > 1 else next(iter(sources), "m3u")
            response = group_response(all_groups, playlist_cached=playlist_cached, source=source)
            if warnings:
                response["warning"] = "; ".join(warnings)
            state.save_group_cache(response)
            return jsonify(response)
        except Exception as exc:
            LOG.exception("playlist fetch failed")
            return json_error(exc)

    @app.post("/api/playlist/text")
    def text_playlist():
        try:
            payload = api_payload()
            text = str(payload.get("text") or "")
            if not text.strip():
                raise ValueError("Playlist text is empty.")
            provider = active_provider_from_payload(payload)
            provider_id = provider_identifier(provider)
            provider_name = provider_label(provider)
            LOG.info("scanning pasted playlist provider=%s bytes=%s", provider_name, len(text.encode("utf-8", errors="replace")))
            groups = tag_groups(state.write_and_scan_playlist(text, provider_id), provider_id, provider_name)
            response = group_response(groups, playlist_cached=True, source="m3u")
            state.save_group_cache(response)
            return jsonify(response)
        except Exception as exc:
            LOG.exception("pasted playlist scan failed")
            return json_error(exc)

    @app.post("/api/playlist/upload")
    def upload_playlist():
        try:
            payload = api_payload()
            upload = request.files.get("file")
            if upload is None:
                raise ValueError("No playlist file was uploaded.")
            provider = active_provider_from_payload(payload)
            provider_id = provider_identifier(provider)
            provider_name = provider_label(provider)
            upload.save(state.playlist_cache_for(provider_id))
            LOG.info("uploaded playlist provider=%s filename=%s", provider_name, upload.filename or "")
            groups = tag_groups(state.scan_cached_playlist(provider_id), provider_id, provider_name)
            response = group_response(groups, playlist_cached=True, source="m3u")
            state.save_group_cache(response)
            return jsonify(response)
        except Exception as exc:
            LOG.exception("playlist upload failed")
            return json_error(exc)

    @app.post("/api/generate")
    def generate():
        try:
            payload = api_payload()
            job = state.start_job(payload)
            return jsonify(job.public())
        except Exception as exc:
            LOG.exception("generator start failed")
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
        LOG.info("cancelling generator job id=%s", job_id)
        job.cancel()
        return jsonify(job.public())

    return app


class AppState:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.work_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.playlists_dir = self.work_dir / "playlists"
        self.playlists_dir.mkdir(parents=True, exist_ok=True)
        self.playlist_cache = self.work_dir / "playlist.m3u"
        self.settings_path = self.work_dir / "web-settings.json"
        self.groups_path = self.work_dir / "web-groups.json"
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def load_settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {}
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOG.warning("settings file is not valid JSON path=%s", self.settings_path)
            return {}
        if not isinstance(data, dict):
            LOG.warning("settings file is not a JSON object path=%s", self.settings_path)
            return {}
        LOG.info("loaded web settings path=%s providers=%s", self.settings_path, len(settings_providers(data.get("settings") or data)))
        return data

    def save_settings(self, payload: dict[str, Any]) -> None:
        self.settings_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        LOG.info(
            "saved web settings path=%s providers=%s",
            self.settings_path,
            len(settings_providers(payload.get("settings") or payload)),
        )

    def load_group_cache(self) -> dict[str, Any]:
        if not self.groups_path.exists():
            return {}
        try:
            data = json.loads(self.groups_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOG.warning("group cache is not valid JSON path=%s", self.groups_path)
            return {}
        if not isinstance(data, dict):
            LOG.warning("group cache is not a JSON object path=%s", self.groups_path)
            return {}
        groups = data.get("groups")
        count = len(groups) if isinstance(groups, list) else 0
        LOG.info("loaded web group cache path=%s groups=%s", self.groups_path, count)
        return data

    def load_or_rebuild_group_cache(self) -> dict[str, Any]:
        cached = self.load_group_cache()
        if cached.get("groups"):
            return cached

        settings = self.load_settings()
        configured = settings.get("settings") if isinstance(settings.get("settings"), dict) else settings
        providers = settings_providers(configured) or [{"id": "default", "name": "Provider 1"}]
        groups: list[dict[str, object]] = []
        for provider in providers:
            provider_id = provider_identifier(provider)
            playlist_cache = self.playlist_cache_for(provider_id)
            if not playlist_cache.exists():
                continue
            provider_name = provider_label(provider)
            LOG.info("rebuilding web group cache from playlist provider=%s path=%s", provider_name, playlist_cache)
            groups.extend(tag_groups(self.scan_cached_playlist(provider_id), provider_id, provider_name))

        if not groups:
            return cached

        response = group_response(groups, playlist_cached=True, source="m3u")
        self.save_group_cache(response)
        return response

    def save_group_cache(self, payload: dict[str, object]) -> None:
        self.groups_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        groups = payload.get("groups")
        count = len(groups) if isinstance(groups, list) else 0
        LOG.info("saved web group cache path=%s groups=%s", self.groups_path, count)

    def playlist_cache_for(self, provider_id: str) -> Path:
        if provider_id == "default":
            return self.playlist_cache
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in provider_id).strip("_")
        return self.playlists_dir / f"{safe or 'default'}.m3u"

    def fetch_and_scan_playlist(self, url: str, user_agent: str, provider_id: str = "default") -> list[dict[str, object]]:
        headers = {"User-Agent": user_agent}
        playlist_cache = self.playlist_cache_for(provider_id)
        try:
            with requests.get(url, stream=True, timeout=(20, 240), headers=headers) as response:
                response.raise_for_status()
                with playlist_cache.open("w", encoding="utf-8", errors="replace", newline="\n") as fh:

                    def lines():
                        for line in response.iter_lines(decode_unicode=True):
                            text = decode_m3u_line(line)
                            fh.write(text + "\n")
                            yield text

                    return [group.to_dict() for group in scan_m3u_groups(lines())]
        except requests.RequestException as exc:
            raise RuntimeError(describe_playlist_fetch_error(exc)) from None

    def fetch_xtream_groups(self, settings: dict[str, Any]) -> list[dict[str, object]]:
        client = XtreamClient(provider_from_settings(settings), timeout=120)
        movie_categories = client.categories("movie")
        series_categories = client.categories("series")
        movies = client.movies()
        series = client.series()
        return xtream_group_summaries(movie_categories, series_categories, movies, series)

    def write_and_scan_playlist(self, text: str, provider_id: str = "default") -> list[dict[str, object]]:
        self.playlist_cache_for(provider_id).write_text(text, encoding="utf-8")
        return self.scan_cached_playlist(provider_id)

    def scan_cached_playlist(self, provider_id: str = "default") -> list[dict[str, object]]:
        with self.playlist_cache_for(provider_id).open("r", encoding="utf-8", errors="replace") as fh:
            return [group.to_dict() for group in scan_m3u_groups(fh)]

    def scan_cached_playlist_if_available(self, provider_id: str = "default") -> list[dict[str, object]] | None:
        playlist_cache = self.playlist_cache_for(provider_id)
        if not playlist_cache.exists():
            return None
        LOG.info("using cached playlist after live fetch failure path=%s", playlist_cache)
        return self.scan_cached_playlist(provider_id)

    def start_job(self, payload: dict[str, Any]) -> "Job":
        settings = dict(payload.get("settings") or {})
        selected_groups = dict(payload.get("selected_groups") or {})
        provider_runs = self.build_provider_runs(settings, selected_groups)

        job_id = uuid.uuid4().hex[:12]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        summary_path = job_dir / "last-run.json"
        log_path = job_dir / "generate.log"

        job = Job(
            job_id=job_id,
            job_dir=job_dir,
            runs=provider_runs,
            summary_path=summary_path,
            log_path=log_path,
        )
        with self.lock:
            self.jobs[job_id] = job
        LOG.info("starting generator job id=%s providers=%s", job_id, len(provider_runs))
        job.start()
        return job

    def get_job(self, job_id: str) -> "Job | None":
        with self.lock:
            return self.jobs.get(job_id)

    def build_provider_runs(self, settings: dict[str, Any], selected_groups: dict[str, Any]) -> list[dict[str, Any]]:
        providers = settings_providers(settings)
        if not providers:
            raise ValueError("At least one provider is required.")
        selected_by_provider = selected_groups_by_provider(selected_groups, providers)
        runs: list[dict[str, Any]] = []
        for index, provider in enumerate(providers):
            provider_id = provider_identifier(provider)
            provider_name = provider_label(provider)
            provider_settings = settings_for_provider(settings, provider)
            validate_generate_settings(provider_settings, provider_name)
            provider_selected = selected_by_provider.get(provider_id) or empty_selected_groups()
            groups_path = self.jobs_dir / "pending-selected-groups.json"
            playlist_cache = self.playlist_cache_for(provider_id)
            if not playlist_cache.exists() and self.playlist_cache.exists() and provider_id == "default":
                playlist_cache = self.playlist_cache
            run = {
                "provider_id": provider_id,
                "provider_name": provider_name,
                "settings": provider_settings,
                "selected_groups": provider_selected,
                "groups_path": groups_path,
                "playlist_cache": playlist_cache if playlist_cache.exists() else None,
                "clean_output": as_bool(provider_settings.get("clean_output")) and index == 0,
            }
            runs.append(run)
        return runs


class Job:
    def __init__(
        self,
        job_id: str,
        job_dir: Path,
        runs: list[dict[str, Any]],
        summary_path: Path,
        log_path: Path,
    ) -> None:
        self.id = job_id
        self.job_dir = job_dir
        self.runs = runs
        self.summary_path = summary_path
        self.log_path = log_path
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
        aggregate: dict[str, Any] = {"providers": [], "provider_count": len(self.runs)}
        with self.log_path.open("w", encoding="utf-8", errors="replace") as log:
            for index, run in enumerate(self.runs, start=1):
                if self.status == "cancelled":
                    break
                provider_id = str(run["provider_id"])
                provider_name = str(run["provider_name"])
                run_dir = self.job_dir / f"{index:02d}-{provider_id}"
                run_dir.mkdir(parents=True, exist_ok=True)
                config_path = run_dir / "config.yml"
                groups_path = run_dir / "selected-groups.json"
                run_summary_path = run_dir / "last-run.json"
                provider_settings = dict(run["settings"])
                provider_settings["clean_output"] = bool(run["clean_output"])
                groups_path.write_text(
                    json.dumps(run["selected_groups"], indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                config = build_config(provider_settings, groups_path, run.get("playlist_cache"))
                config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
                env = job_environment(provider_settings)
                command = [
                    sys.executable,
                    "-m",
                    "vod_strm_builder.cli",
                    "generate",
                    "--config",
                    str(config_path),
                    "--summary-json",
                    str(run_summary_path),
                ]
                banner = f"Running provider {index}/{len(self.runs)}: {provider_name}\n"
                log.write(banner)
                log.write("Running: vod-strm-builder generate --config config.yml --summary-json last-run.json\n\n")
                log.flush()
                LOG.info("job %s provider start provider=%s config=%s", self.id, provider_name, config_path)
                self.process = subprocess.Popen(
                    command,
                    cwd=run_dir,
                    env={**os.environ, **env},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    log.write(line)
                    log.flush()
                    LOG.info("job %s provider=%s %s", self.id, provider_name, line.rstrip())
                self.return_code = self.process.wait()
                provider_summary = read_json_file(run_summary_path)
                aggregate["providers"].append(
                    {
                        "provider_id": provider_id,
                        "provider_name": provider_name,
                        "return_code": self.return_code,
                        "summary": provider_summary,
                    }
                )
                LOG.info("job %s provider finished provider=%s return_code=%s", self.id, provider_name, self.return_code)
                if self.return_code != 0:
                    break
                log.write("\n")
            self.summary_path.write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.ended_at = time.time()
        if self.status == "cancelled":
            LOG.info("job %s cancelled", self.id)
            return
        self.status = "complete" if self.return_code == 0 else "failed"
        LOG.info("job %s finished status=%s return_code=%s", self.id, self.status, self.return_code)

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
            "provider_count": len(self.runs),
            "summary_path": str(self.summary_path),
            "log_path": str(self.log_path),
            "summary": self.read_summary(),
        }


def frontend_path() -> Path:
    path = Path(__file__).resolve().parents[2] / "web" / "group-picker.html"
    if not path.exists():
        raise FileNotFoundError("Frontend file web/group-picker.html was not found.")
    return path


def group_response(
    groups: list[dict[str, object]],
    playlist_cached: bool,
    source: str = "m3u",
    warning: str | None = None,
) -> dict[str, object]:
    movie_total = sum(int(group["movie_count"]) for group in groups)
    series_total = sum(int(group["series_count"]) for group in groups)
    live_total = sum(int(group["live_count"]) for group in groups)
    payload: dict[str, object] = {
        "groups": groups,
        "playlist_cached": playlist_cached,
        "source": source,
        "stats": {
            "groups": len(groups),
            "movie_entries": movie_total,
            "series_entries": series_total,
            "live_entries": live_total,
        },
    }
    if warning:
        payload["warning"] = warning
    return payload


def settings_providers(settings: dict[str, Any]) -> list[dict[str, Any]]:
    raw_providers = settings.get("providers")
    if isinstance(raw_providers, list):
        providers = [dict(item) for item in raw_providers if isinstance(item, dict)]
    else:
        providers = [
            {
                "id": clean_string(settings.get("provider_id")) or "default",
                "name": clean_string(settings.get("provider_name")) or "Provider 1",
                "server_url": settings.get("server_url"),
                "username": settings.get("username"),
                "password": settings.get("password"),
                "m3u_url": settings.get("m3u_url"),
                "user_agent": settings.get("user_agent"),
            }
        ]
    normalized: list[dict[str, Any]] = []
    for index, provider in enumerate(providers, start=1):
        item = dict(provider)
        item["id"] = clean_string(item.get("id")) or f"provider-{index}"
        item["name"] = clean_string(item.get("name")) or f"Provider {index}"
        item["server_url"] = clean_string(item.get("server_url"))
        item["username"] = clean_string(item.get("username"))
        item["password"] = clean_string(item.get("password"))
        item["m3u_url"] = clean_string(item.get("m3u_url"))
        item["user_agent"] = clean_string(item.get("user_agent")) or DEFAULT_USER_AGENT
        if any(item.get(key) for key in ("server_url", "username", "password", "m3u_url")):
            normalized.append(item)
    return normalized


def active_provider_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    providers = settings_providers(payload)
    active_id = clean_string(payload.get("active_provider_id"))
    for provider in providers:
        if provider_identifier(provider) == active_id:
            return provider
    return providers[0] if providers else {"id": "default", "name": "Provider 1", "user_agent": DEFAULT_USER_AGENT}


def settings_for_provider(settings: dict[str, Any], provider: dict[str, Any]) -> dict[str, Any]:
    provider_settings = {key: value for key, value in settings.items() if key != "providers"}
    provider_settings.update(provider)
    provider_settings["provider_id"] = provider_identifier(provider)
    provider_settings["provider_name"] = provider_label(provider)
    return provider_settings


def provider_identifier(provider: dict[str, Any]) -> str:
    return clean_string(provider.get("id")) or "default"


def provider_label(provider: dict[str, Any]) -> str:
    return clean_string(provider.get("name")) or clean_string(provider.get("server_url")) or provider_identifier(provider)


def tag_groups(
    groups: list[dict[str, object]],
    provider_id: str,
    provider_name: str,
    provider_source: str = "m3u",
) -> list[dict[str, object]]:
    tagged = []
    for group in groups:
        item = dict(group)
        item["provider_id"] = provider_id
        item["provider_name"] = provider_name
        item["provider_source"] = provider_source
        item["key"] = f"{provider_id}::{item.get('name', '')}"
        tagged.append(item)
    return tagged


def empty_selected_groups() -> dict[str, list[str]]:
    return {
        "movie_groups": [],
        "series_groups": [],
        "movie_category_ids": [],
        "series_category_ids": [],
    }


def selected_groups_by_provider(
    selected_groups: dict[str, Any],
    providers: list[dict[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    provider_ids = [provider_identifier(provider) for provider in providers]
    raw = selected_groups.get("providers")
    if isinstance(raw, dict):
        result: dict[str, dict[str, list[str]]] = {}
        for provider_id in provider_ids:
            provider_groups = raw.get(provider_id) if isinstance(raw.get(provider_id), dict) else {}
            result[provider_id] = {
                "movie_groups": string_list(provider_groups.get("movie_groups")),
                "series_groups": string_list(provider_groups.get("series_groups")),
                "movie_category_ids": string_list(provider_groups.get("movie_category_ids")),
                "series_category_ids": string_list(provider_groups.get("series_category_ids")),
            }
        return result
    first = provider_ids[0] if provider_ids else "default"
    return {
        first: {
            "movie_groups": string_list(selected_groups.get("movie_groups")),
            "series_groups": string_list(selected_groups.get("series_groups")),
            "movie_category_ids": string_list(selected_groups.get("movie_category_ids")),
            "series_category_ids": string_list(selected_groups.get("series_category_ids")),
        }
    }


def provider_from_settings(settings: dict[str, Any]) -> ProviderConfig:
    server_url = provider_server_url(settings)
    username = clean_string(settings.get("username"))
    password = clean_string(settings.get("password"))
    if not server_url or not username or not password:
        raise ValueError("Provider server URL, username, and password are required for Xtream API fallback.")
    return ProviderConfig(
        server_url=server_url,
        username=username,
        password=password,
        user_agent=clean_string(settings.get("user_agent")) or DEFAULT_USER_AGENT,
    )


def provider_server_url(settings: dict[str, Any]) -> str:
    server_url = clean_string(settings.get("server_url")).rstrip("/")
    if server_url:
        return server_url
    m3u_url = clean_string(settings.get("m3u_url"))
    parsed = urlparse(m3u_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return ""


def xtream_group_summaries(
    movie_categories: dict[str, str],
    series_categories: dict[str, str],
    movies: list[MovieItem],
    series: list[SeriesItem],
) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    samples: dict[str, set[str]] = defaultdict(set)

    def group_name(categories: dict[str, str], category_id: str) -> str:
        return (categories.get(str(category_id)) or f"Category {category_id or 'unknown'}").strip()

    def ensure(name: str) -> dict[str, object]:
        key = name or "Ungrouped"
        return groups.setdefault(
            key,
            {"name": key, "movie_count": 0, "series_count": 0, "live_count": 0},
        )

    for category_id, name in movie_categories.items():
        ensure(name or f"Category {category_id}")
    for category_id, name in series_categories.items():
        ensure(name or f"Category {category_id}")
    for item in movies:
        name = group_name(movie_categories, item.category_id)
        group = ensure(name)
        group["movie_count"] = int(group["movie_count"]) + 1
        if len(samples[name]) < 8:
            samples[name].add(item.name)
    for item in series:
        name = group_name(series_categories, item.category_id)
        group = ensure(name)
        group["series_count"] = int(group["series_count"]) + 1
        if len(samples[name]) < 8:
            samples[name].add(item.name)

    for name, group in groups.items():
        group["total"] = int(group["movie_count"]) + int(group["series_count"]) + int(group["live_count"])
        group["samples"] = sorted(samples[name])[:3]
    return sorted(groups.values(), key=lambda group: str(group["name"]).lower())


def build_config(settings: dict[str, Any], groups_path: Path, playlist_cache: Path | None) -> dict[str, Any]:
    provider: dict[str, Any] = {
        "server_url": provider_server_url(settings),
        "username_env": "XTREAM_USERNAME",
        "password_env": "XTREAM_PASSWORD",
        "user_agent": clean_string(settings.get("user_agent")) or DEFAULT_USER_AGENT,
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
            "source": clean_string(settings.get("series_source")) or "m3u",
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


def validate_generate_settings(settings: dict[str, Any], provider_name: str = "Provider") -> None:
    required = {
        "username": "Provider username",
        "password": "Provider password",
        "movies_dir": "Movies directory",
        "series_dir": "TV directory",
    }
    missing = [label for key, label in required.items() if not clean_string(settings.get(key))]
    if not provider_server_url(settings):
        missing.insert(0, "Provider server URL")
    if missing:
        raise ValueError(f"{provider_name}: missing required settings: " + ", ".join(missing))
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


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def redact_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "<custom>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def json_error(exc: Exception):
    return jsonify({"error": str(exc)}), 400


def api_payload() -> dict[str, Any]:
    if request.form:
        raw = request.form.get("payload")
        if raw:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Request form payload must be a JSON object.")
            return payload
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


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
