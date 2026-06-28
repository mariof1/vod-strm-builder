from __future__ import annotations

import re
import unicodedata
from pathlib import Path

INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LANG_PREFIX = re.compile(r"^(?:[A-Z]{2,4}|NF|AMZN|DSNP|ATVP|HBO|QFR|MULTI)\s*-\s+", re.IGNORECASE)
TRAILING_YEAR = re.compile(r"\((19\d{2}|20\d{2}|21\d{2})\)\s*$")
BARE_TRAILING_YEAR = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\s*$")


def clean_title(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"^\((?:MULTI|EN|FR|DE|PL|ES|IT)\)\s*", "", text, flags=re.IGNORECASE)
    text = LANG_PREFIX.sub("", text).strip()
    return re.sub(r"\s+", " ", text)


def extract_year(name: str, *candidates: object) -> int | None:
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        match = re.search(r"(19\d{2}|20\d{2}|21\d{2})", str(candidate))
        if match:
            return int(match.group(1))
    match = TRAILING_YEAR.search(name or "") or BARE_TRAILING_YEAR.search(name or "")
    return int(match.group(1)) if match else None


def strip_redundant_year(name: str, year: int | None) -> str:
    text = name.strip()
    match = TRAILING_YEAR.search(text)
    if match and year and int(match.group(1)) == year:
        text = text[: match.start()].rstrip()
    return text


def safe_filename(name: str, max_len: int = 180) -> str:
    text = INVALID_FILENAME.sub("", name or "Unknown")
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return (text or "Unknown")[:max_len].rstrip(". ")


def folder_name(name: str, year: int | None, tmdb_id: str | None, append_tmdb: bool) -> str:
    base_title = strip_redundant_year(clean_title(name), year)
    base = f"{safe_filename(base_title)} ({year})" if year else safe_filename(base_title)
    if append_tmdb and tmdb_id:
        return f"{base} {{tmdb-{tmdb_id}}}"
    return base


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = text.replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def qualityless_name(name: str, words: tuple[str, ...]) -> str:
    text = normalize_name(name)
    for word in words:
        text = re.sub(rf"\b{re.escape(word.casefold())}\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def ensure_empty_dir(path: Path, dry_run: bool) -> None:
    if dry_run:
        return
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            import shutil

            shutil.rmtree(child)
        else:
            child.unlink()

