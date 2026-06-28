from __future__ import annotations

import re
import unicodedata
from pathlib import Path

INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LEADING_TAG = re.compile(
    r"^(?:"
    r"\|?[A-Z]{2,4}\|?"
    r"|\[[A-Z]{2,4}\]"
    r"|\([A-Z]{2,4}\)"
    r"|NF|A\+|D\+|AMZN|DSNP|ATVP|HBO|MAX|QFR|MULTI"
    r")\s*[-:|]\s+",
    re.IGNORECASE,
)
TRAILING_YEAR = re.compile(r"\((19\d{2}|20\d{2}|21\d{2})\)\s*$")
BARE_TRAILING_YEAR = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\s*$")
BRACKETED_YEAR = re.compile(r"\((19\d{2}|20\d{2}|21\d{2})\)")
QUALITY_TOKEN = re.compile(
    r"\b(?:"
    r"4k|8k|uhd|fhd|hd|sd|hdr|hdr10|dv|dolby\s+vision|"
    r"web[- .]?dl|webrip|bluray|blu[- .]?ray|brrip|hdrip|hdtv|"
    r"x264|x265|h\.?264|h\.?265|hevc|aac|ddp?|atmos"
    r")\b",
    re.IGNORECASE,
)
METADATA_FRAGMENT_WORDS = {
    "audio",
    "dub",
    "dubbed",
    "dual",
    "eng",
    "english",
    "french",
    "german",
    "ita",
    "italian",
    "japanese",
    "multi",
    "pl",
    "polish",
    "spanish",
    "sub",
    "subs",
    "subtitle",
    "subtitles",
    "truefrench",
    "vostfr",
}
TRAILING_METADATA = re.compile(
    r"(?:[-\s]+(?:multi|eng|english|sub|subs|subtitle|subtitles|dual|dubbed?|audio|4k|uhd|fhd|hd))*\s*$",
    re.IGNORECASE,
)


def clean_title(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = text.replace("–", "-").replace("—", "-")
    text = _strip_leading_tags(text)
    text = _truncate_after_bracketed_year(text)
    text = re.sub(r"[\[(]([^\])()]*)[\])]", _drop_metadata_fragment, text)
    text = QUALITY_TOKEN.sub(" ", text)
    text = TRAILING_METADATA.sub("", text)
    text = re.sub(r"\s+([),.:;!?])", r"\1", text)
    text = re.sub(r"([([])\s+", r"\1", text)
    text = re.sub(r"\s*-\s*(?=\(|$)", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -._")


def extract_year(name: str, *candidates: object) -> int | None:
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        match = re.search(r"(19\d{2}|20\d{2}|21\d{2})", str(candidate))
        if match:
            return int(match.group(1))
    match = BRACKETED_YEAR.search(name or "") or TRAILING_YEAR.search(name or "") or BARE_TRAILING_YEAR.search(name or "")
    return int(match.group(1)) if match else None


def strip_redundant_year(name: str, year: int | None) -> str:
    text = name.strip()
    match = TRAILING_YEAR.search(text)
    if match and year and int(match.group(1)) == year:
        text = text[: match.start()].rstrip()
    return text


def safe_filename(name: str, max_len: int = 180) -> str:
    text = (name or "Unknown").replace(":", " - ").replace("/", "-").replace("\\", "-").replace("|", "-")
    text = INVALID_FILENAME.sub("", text)
    text = re.sub(r"\s+-\s+", " - ", text)
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


def _strip_leading_tags(text: str) -> str:
    cleaned = text.strip()
    while True:
        stripped = re.sub(r"^\((?:MULTI|EN|FR|DE|PL|ES|IT)\)\s*", "", cleaned, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"^\[(?:MULTI|EN|FR|DE|PL|ES|IT)\]\s*", "", stripped, flags=re.IGNORECASE).strip()
        stripped = LEADING_TAG.sub("", stripped).strip()
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


def _truncate_after_bracketed_year(text: str) -> str:
    match = BRACKETED_YEAR.search(text)
    if not match:
        return text
    return text[: match.end()].strip()


def _drop_metadata_fragment(match: re.Match[str]) -> str:
    fragment = (match.group(1) or "").strip()
    if re.fullmatch(r"19\d{2}|20\d{2}|21\d{2}", fragment):
        return f"({fragment})"
    words = {word.casefold() for word in re.findall(r"[A-Za-z]+", fragment)}
    if words and words.issubset(METADATA_FRAGMENT_WORDS):
        return " "
    if any(term in fragment.casefold() for term in ("multi sub", "eng-sub", "eng sub", "multi audio")):
        return " "
    return match.group(0)
