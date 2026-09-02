"""Episode-aware Sdilej.cz discovery built on the shared transfer primitives."""

from __future__ import annotations

import re
import time
from dataclasses import replace

import requests
from sdilej_to_prehrajto.language import LanguageDetectionError, WhisperLanguageDetector
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import (
    language_tier,
    quality_acceptable,
    rank_candidates,
    resolution_label,
    resolution_rank,
)
from sdilej_to_prehrajto.sdilej import (
    BASE_URL,
    SdilejError,
    audio_language_hint,
    login,
    parse_detail_html,
    parse_search_html,
    probe_media,
    slugify,
)

from .models import Episode


EPISODE_CODE_RE = re.compile(r"\bS(?P<season>\d{1,2})E(?P<episode>\d{1,3})\b|\b(?P<sx>\d{1,2})x(?P<ex>\d{1,3})\b", re.I)
NOISE_RE = re.compile(r"\b(?:1080p|720p|2160p|4k|bluray|webrip|web[ ._-]?dl|hdtv|x26[45]|hevc|av1|cz|cs|sk|eng|dabing|titulky|mkv|mp4)\b", re.I)


def normalize(value: str) -> str:
    value = NOISE_RE.sub(" ", value.casefold())
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", value)).strip()


def has_exact_code(value: str, episode: Episode) -> bool:
    for match in EPISODE_CODE_RE.finditer(value):
        season = int(match.group("season") or match.group("sx"))
        number = int(match.group("episode") or match.group("ex"))
        if (season, number) == (episode.season, episode.number):
            return True
    return False


def episode_match(episode: Episode, candidate_title: str) -> tuple[MatchTier, dict]:
    codes = {
        (int(match.group("season") or match.group("sx")), int(match.group("episode") or match.group("ex")))
        for match in EPISODE_CODE_RE.finditer(candidate_title)
    }
    code_matches = (episode.season, episode.number) in codes
    aliases = [title for title in (episode.series_title, episode.series_original_title) if title]
    normalized_candidate = normalize(candidate_title)
    title_matches = [normalize(alias) in normalized_candidate for alias in aliases if len(normalize(alias)) >= 3]
    evidence = {
        "expected_episode": episode.code,
        "episode_code_match": code_matches,
        "series_alias_match": any(title_matches),
    }
    evidence["episode_codes_found"] = [f"S{season:02d}E{number:02d}" for season, number in sorted(codes)]
    if len(codes) > 1:
        evidence["reason"] = "multiple_episode_codes"
        return MatchTier.REJECT, evidence
    if code_matches and any(title_matches):
        return MatchTier.STRONG, evidence
    evidence["reason"] = "missing_series_or_episode_identity"
    return MatchTier.REJECT, evidence


def display_name(episode: Episode, candidate: Candidate) -> str:
    title = f"{episode.series_title} {episode.code}"
    if episode.title and normalize(episode.title) != normalize(episode.series_title):
        title += f" - {episode.title}"
    title += f" {resolution_label(candidate.width, candidate.height)}"
    if candidate.language_tier == LanguageTier.CZECH_AUDIO:
        return title + " CZ Dabing"
    if candidate.language_tier == LanguageTier.SLOVAK_AUDIO:
        return title + " SK Dabing"
    return title + " CZ Titulky"


class EpisodeSourceProvider:
    """Find, verify and later refresh stable episode detail URLs.

    The manifest stores only ``candidate.url``. ``download_url`` and
    ``sample_url`` are per-session credentials reconstructed immediately before
    upload and are intentionally excluded from persisted records.
    """

    def __init__(self, session: requests.Session, *, detector=None, request_gap_seconds: float = 2.0):
        self.session = session
        self.detector = detector or WhisperLanguageDetector()
        self.request_gap_seconds = request_gap_seconds
        self._last_request = 0.0

    @classmethod
    def authenticated(cls, email: str, password: str, **kwargs) -> "EpisodeSourceProvider":
        return cls(login(email, password), **kwargs)

    def _get(self, url: str, *, session: requests.Session | None = None) -> requests.Response:
        active_session = session or self.session
        if session is None:
            delay = self.request_gap_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
        try:
            response = active_session.get(url, timeout=45)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            raise SdilejError("Sdilej.cz request failed") from error
        finally:
            if session is None:
                self._last_request = time.monotonic()

    def search(self, episode: Episode) -> list[Candidate]:
        candidates: dict[str, Candidate] = {}
        for title in dict.fromkeys((episode.series_title, episode.series_original_title)):
            if not title:
                continue
            query = f"{title} {episode.code}"
            url = f"{BASE_URL}/{slugify(query)}/s/-6"
            for candidate in parse_search_html(self._get(url).text, query=query):
                tier, evidence = episode_match(episode, candidate.title)
                candidate.match_tier = tier
                candidate.match_evidence = evidence
                if tier in (MatchTier.STRONG, MatchTier.SOLID):
                    candidates.setdefault(candidate.source_id, candidate)
        return list(candidates.values())

    def _verify(self, episode: Episode, candidate: Candidate) -> Candidate | None:
        detail = parse_detail_html(self._get(candidate.url).text, candidate)
        media = probe_media(detail.download_url)
        detail = replace(
            detail,
            video_codec=media.get("video_codec") or detail.video_codec,
            width=int(media.get("width") or detail.width),
            height=int(media.get("height") or detail.height),
            duration_sec=int(media.get("duration_sec") or detail.duration_sec or 0),
        )
        tier, evidence = episode_match(episode, detail.title)
        if tier not in (MatchTier.STRONG, MatchTier.SOLID) or not quality_acceptable(detail):
            return None
        language, probability = self.detector.detect(detail.sample_url)
        hint = audio_language_hint(detail.filename)
        if hint and language_tier(language) != language_tier(hint):
            consensus = getattr(self.detector, "detect_consensus", None)
            if consensus:
                language, probability = consensus(detail.sample_url, detail.duration_sec, initial=(language, probability), preferred_language=hint)
        if probability < 0.65:
            raise LanguageDetectionError("Whisper language confidence is too low")
        return replace(
            detail,
            match_tier=tier,
            match_evidence=evidence,
            audio_language=language,
            language_probability=probability,
            language_evidence="whisper_remote_sample",
            language_tier=language_tier(language),
        )

    def discover(self, episode: Episode) -> Candidate | None:
        candidates = self.search(episode)
        by_resolution: dict[int, list[Candidate]] = {}
        for candidate in candidates:
            by_resolution.setdefault(resolution_rank(candidate.width, candidate.height), []).append(candidate)
        resolved: list[Candidate] = []
        for resolution in sorted(by_resolution, reverse=True):
            unresolved = False
            for candidate in sorted(by_resolution[resolution], key=lambda item: (item.size_bytes or 0, item.source_id)):
                detail = None
                verification_completed = False
                for _attempt in range(2):
                    try:
                        detail = self._verify(episode, candidate)
                        verification_completed = True
                        break
                    except (SdilejError, LanguageDetectionError, requests.RequestException):
                        continue
                if not verification_completed:
                    unresolved = True
                    continue
                if detail:
                    resolved.append(detail)
                    # Candidates in this resolution tier are ordered by size.
                    # Once Czech audio succeeds, no later source in the tier
                    # can be a smaller Czech candidate. Keep scanning only if
                    # an earlier source could not be verified safely.
                    if detail.language_tier == LanguageTier.CZECH_AUDIO and not unresolved:
                        return rank_candidates(resolved)[0]
            # Do not silently downgrade while a higher-quality candidate could
            # not be verified; retry that episode in a later preparation pass.
            if unresolved:
                return None
        ranked = rank_candidates(resolved)
        return ranked[0] if ranked else None

    def refresh(self, candidate: Candidate, *, session: requests.Session) -> Candidate:
        return parse_detail_html(self._get(candidate.url, session=session).text, candidate)
