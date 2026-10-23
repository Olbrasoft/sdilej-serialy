"""Extract existing Czech text subtitles from an exact Sdilej source only."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from sdilej_to_prehrajto.models import Candidate
from .source_detail import parse_detail_html, resolve_original

MAX_SUBTITLE_BYTES = 8 * 1024 * 1024
CZECH = {"cs", "cz", "ces", "cze", "czech", "čeština", "česky", "české"}
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}


class SubtitleUnavailable(RuntimeError):
    """A fixed, non-sensitive reason suitable for durable reports."""


def zero_original_evidence(session, candidate, *, deadline=None):
    """Exclude only a repeatedly unprobeable original with zero-filled ends."""
    try:
        page = session.get(candidate.url, timeout=extraction_timeout(45, deadline))
        page.raise_for_status()
        if urlsplit(page.url).path.split('/')[1:2] != [candidate.source_id]:
            return None
        original = resolve_original(session, parse_detail_html(page.text, candidate))
        size = original.size_bytes
        if not size or size < 16384:
            return None
        for start in (0, size // 2, size - 4096):
            end = start + 4095
            with session.get(original.download_url, headers={'Range': f'bytes={start}-{end}',
                             'Referer': candidate.url}, stream=True,
                             timeout=extraction_timeout(30, deadline)) as response:
                if (response.status_code != 206
                        or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}'):
                    return None
                payload = bytearray()
                for chunk in response.iter_content(4096):
                    payload.extend(chunk)
                    if len(payload) >= 4096:
                        break
                if len(payload) != 4096 or (start != size // 2 and any(payload)):
                    return None
        return dict(source_id=candidate.source_id, size_bytes=size,
                    reason='zero_header_tail_original', ranges_checked=3, zero_end_ranges=2,
                    checked_at=datetime.now(UTC).isoformat())
    except Exception:
        # Lack of proof is never permission to skip an unresolved original.
        return None


def is_czech(value: str) -> bool:
    return (value or "").strip().casefold().replace("_", "-").split("-")[0] in CZECH


def normalize_srt(payload: bytes) -> bytes:
    """Validate SRT/WebVTT cues and serialize UTF-8 SRT with CRLF endings."""
    if len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleUnavailable("subtitle_too_large")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubtitleUnavailable("subtitle_encoding_unsupported") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\x00" in text:
        raise SubtitleUnavailable("subtitle_invalid")
    timestamp = r"(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}"
    timing = re.compile(rf"^({timestamp})\s+-->\s+({timestamp})(?:\s+[^\n]*)?$")
    cues = []
    previous_start = -1

    def stamp(value):
        parts = value.replace(",", ".").split(":")
        if len(parts) == 2:
            parts.insert(0, "00")
        hours, minutes = int(parts[0]), int(parts[1])
        seconds, millis = map(int, parts[2].split("."))
        if minutes >= 60 or seconds >= 60:
            raise SubtitleUnavailable("subtitle_invalid_timing")
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis, f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"

    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        index = 0 if "-->" in lines[0] else 1
        if index >= len(lines):
            raise SubtitleUnavailable("subtitle_invalid")
        match = timing.fullmatch(lines[index].strip())
        if not match or index + 1 >= len(lines):
            raise SubtitleUnavailable("subtitle_invalid")
        start, start_text = stamp(match[1])
        end, end_text = stamp(match[2])
        if start < previous_start or end <= start:
            raise SubtitleUnavailable("subtitle_invalid_timing")
        previous_start = start
        body = "\r\n".join(lines[index + 1:]).strip()
        if not body:
            raise SubtitleUnavailable("subtitle_empty_cue")
        cues.append(f"{len(cues) + 1}\r\n{start_text} --> {end_text}\r\n{body}")
    if not cues:
        raise SubtitleUnavailable("subtitle_empty")
    return ("\r\n\r\n".join(cues) + "\r\n\r\n").encode("utf-8")


def run_media(arguments: list[str], timeout: int) -> str:
    # Never expose stderr or CalledProcessError: they can contain signed URLs.
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SubtitleUnavailable("source_media_timeout") from None
    if result.returncode:
        raise SubtitleUnavailable("source_media_failed")
    return result.stdout.decode("utf-8")


def convert_text_subtitle(payload: bytes) -> bytes:
    """Convert existing ASS/SSA dialogue, never transcribe or translate it."""
    if len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleUnavailable("subtitle_too_large")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubtitleUnavailable("subtitle_encoding_unsupported") from None
    if "[Script Info]" not in text or "[Events]" not in text:
        return normalize_srt(payload)
    with tempfile.TemporaryDirectory(prefix="subtitle-format-") as directory:
        original = Path(directory) / "original.ass"
        output = Path(directory) / "cs.srt"
        original.write_bytes(payload)
        run_media(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file",
                   "-i", str(original), "-c:s", "srt", "-fs", str(MAX_SUBTITLE_BYTES), str(output)], 30)
        if output.stat().st_size >= MAX_SUBTITLE_BYTES:
            raise SubtitleUnavailable("subtitle_too_large")
        return normalize_srt(output.read_bytes())


def choose_stream(streams: list[dict]) -> dict:
    czech = [stream for stream in streams if is_czech(stream.get("tags", {}).get("language", ""))]
    full = [stream for stream in czech if not stream.get("disposition", {}).get("forced")
            and not re.search(r"forced|commentary|komentář|vynucené", stream.get("tags", {}).get("title", ""), re.I)]
    text = [stream for stream in full if stream.get("codec_name") in TEXT_CODECS]
    if not text:
        raise SubtitleUnavailable("source_czech_text_missing" if not czech else "source_czech_full_text_unsupported")
    # Prefer the source's default full track; never substitute another language.
    return sorted(text, key=lambda stream: (-int(stream.get("disposition", {}).get("default", 0)), int(stream["index"])))[0]


def extraction_timeout(maximum: float, deadline: float | None) -> float:
    remaining = maximum if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise SubtitleUnavailable("batch_deadline_reached")
    return min(maximum, remaining)


def extract_original(session, source: dict, *, deadline: float | None = None) -> tuple[bytes, dict]:
    try:
        return _extract_original(session, source, deadline=deadline)
    except (SubtitleUnavailable, requests.RequestException, TimeoutError):
        if deadline is not None and time.monotonic() >= deadline:
            raise SubtitleUnavailable("batch_deadline_reached") from None
        raise


def _extract_original(session, source: dict, *, deadline: float | None) -> tuple[bytes, dict]:
    source_id, url = str(source["source_id"]), source["source_url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "sdilej.cz" or parsed.path.split("/")[1] != source_id:
        raise SubtitleUnavailable("source_provenance_invalid")
    response = session.get(url, timeout=extraction_timeout(45, deadline))
    response.raise_for_status()
    if urlsplit(response.url).path.split("/")[1] != source_id:
        raise SubtitleUnavailable("source_detail_changed")
    soup = BeautifulSoup(response.text, "html.parser")
    # Only tracks embedded in this exact source page, never search results.
    for track in soup.select("video track[src]"):
        if track.get("kind", "subtitles") not in {"subtitles", "captions"} or not is_czech(track.get("srclang", "")):
            continue
        if re.search(r"forced|vynucené|commentary", track.get("label", ""), re.I):
            continue
        track_url = urljoin(response.url, track["src"])
        host = urlsplit(track_url).hostname or ""
        if urlsplit(track_url).scheme != "https" or not (host == "sdilej.cz" or host.endswith(".sdilej.cz")):
            continue
        with session.get(track_url, stream=True, timeout=extraction_timeout(45, deadline)) as subtitle:
            subtitle.raise_for_status()
            chunks, size = [], 0
            for chunk in subtitle.iter_content(64 * 1024):
                extraction_timeout(45, deadline)
                size += len(chunk)
                if size > MAX_SUBTITLE_BYTES:
                    raise SubtitleUnavailable("subtitle_too_large")
                chunks.append(chunk)
        return convert_text_subtitle(b"".join(chunks)), {"method": "original_page_track", "source_id": source_id}

    detail = parse_detail_html(response.text, Candidate(source_id, url, source.get("source_filename", source_id)))
    with session.get(detail.download_url, headers={"Range": "bytes=0-0", "Referer": url}, stream=True,
                     timeout=(extraction_timeout(20, deadline), extraction_timeout(30, deadline))) as original:
        original.raise_for_status()
        resolved = original.url
    metadata = json.loads(run_media([
        "ffprobe", "-v", "error", "-rw_timeout", "20000000", "-select_streams", "s",
        "-show_streams", "-of", "json", resolved,
    ], extraction_timeout(90, deadline)))
    stream = choose_stream(metadata.get("streams", []))
    with tempfile.TemporaryDirectory(prefix="original-subtitle-") as directory:
        output = Path(directory) / "cs.srt"
        run_media([
            "ffmpeg", "-nostdin", "-v", "error", "-rw_timeout", "20000000", "-i", resolved,
            "-map", f"0:{int(stream['index'])}", "-c:s", "srt", "-fs", str(MAX_SUBTITLE_BYTES), str(output),
        ], extraction_timeout(900, deadline))
        if output.stat().st_size >= MAX_SUBTITLE_BYTES:
            raise SubtitleUnavailable("subtitle_too_large")
        payload = normalize_srt(output.read_bytes())
    return payload, {"method": "original_embedded_track", "source_id": source_id,
                     "stream_index": int(stream["index"]), "source_codec": stream["codec_name"]}
