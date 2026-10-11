"""Episode-aware Sdilej.cz discovery built on the shared transfer primitives."""

from __future__ import annotations

import re
import json
import subprocess
import threading
import time
import unicodedata
from dataclasses import replace
from contextlib import nullcontext
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import (
    language_tier,
    resolution_label,
    resolution_rank,
)
from sdilej_to_prehrajto.sdilej import (
    BASE_URL,
    SdilejError,
    audio_language_hint,
    login,
    parse_search_html,
    probe_media,
    slugify,
)

from .models import Episode
from .source_detail import parse_detail_html, resolve_original, sampled_content_fingerprint
from .quality import quality_acceptable, rank_candidates
from .numbering import mapped_episode_title
from .auth import login_with_retry
from .source_cache import media_key, stable_url, search_url
from .audio_pipeline import PipelinedLanguageDetector


# Underscores are filename separators, not letters adjoining an episode code.
# Keep alphanumeric boundaries so embedded or truncated codes cannot match.
EPISODE_CODE_RE = re.compile(r"(?<![^\W_])(?:S(?P<season>\d{1,2})E(?P<episode>\d{1,3})|(?P<sx>\d{1,2})x(?P<ex>\d{1,3}))(?![^\W_])", re.I)
NOISE_RE = re.compile(r"\b(?:1080p|720p|2160p|4k|bluray|webrip|web[ ._-]?dl|hdtv|x26[45]|hevc|av1|cz|cs|sk|eng|dabing|titulky|mkv|mp4)\b", re.I)


def normalize(value: str) -> str:
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )
    value = NOISE_RE.sub(" ", value.casefold().replace('_', ' '))
    return re.sub(r"\s+", " ", re.sub(r"[\W_]+", " ", value)).strip()


def series_aliases(episode: Episode) -> tuple[str, ...]:
    aliases = [title for title in (episode.series_title, episode.series_original_title) if title]
    # The catalog spells the acronym with asterisks, while many releases and
    # search results use a single word. Do not collapse arbitrary series words.
    if any(normalize(title) == 'm a s h' for title in aliases):
        aliases.append('MASH')
    return tuple(dict.fromkeys(aliases))


def series_identity_match(episode: Episode, candidate_title: str, code_start: int) -> tuple[bool, str]:
    """Require the pre-episode prefix to consist only of known series aliases.

    This prevents a base title such as ``Planet Earth`` from matching the
    distinct sequel ``Planet Earth II`` while still accepting a filename that
    contains both Czech and original aliases plus a release year.
    """
    remaining = normalize(candidate_title[:code_start])
    remaining = re.sub(r"\b(?:19|20)\d{2}\b", " ", remaining)
    matched = False
    aliases = sorted(
        {normalize(title) for title in series_aliases(episode)},
        key=len,
        reverse=True,
    )
    for alias in aliases:
        pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
        remaining, count = re.subn(pattern, " ", remaining)
        matched = matched or bool(count)
    remaining = re.sub(r"\s+", " ", remaining).strip()
    return matched and not remaining, remaining


def has_exact_code(value: str, episode: Episode) -> bool:
    for match in EPISODE_CODE_RE.finditer(value):
        season = int(match.group("season") or match.group("sx"))
        number = int(match.group("episode") or match.group("ex"))
        if (season, number) == (episode.season, episode.number):
            return True
    return False


def runtime_acceptable(episode: Episode, duration_sec: int | None) -> bool:
    """Reject a same-title remake whose episode duration is clearly different."""
    if not episode.runtime_min or not duration_sec:
        return True
    expected = episode.runtime_min * 60
    tolerance = max(8 * 60, int(expected * 0.35))
    return abs(duration_sec - expected) <= tolerance


def episode_match(episode: Episode, candidate_title: str) -> tuple[MatchTier, dict]:
    mapped = (mapped_episode_title(episode, candidate_title, normalize)
              if len(list(EPISODE_CODE_RE.finditer(candidate_title))) <= 1 else None)
    if mapped:
        tier, evidence = episode_match(episode, mapped)
        evidence['numbering_alias'] = candidate_title
        return tier, evidence
    code_matches_found = list(EPISODE_CODE_RE.finditer(candidate_title))
    codes = {
        (int(match.group("season") or match.group("sx")), int(match.group("episode") or match.group("ex")))
        for match in code_matches_found
    }
    code_matches = (episode.season, episode.number) in codes
    title_matches, unmatched_prefix = series_identity_match(
        episode,
        candidate_title,
        code_matches_found[0].start() if code_matches_found else len(candidate_title),
    )
    evidence = {
        "expected_episode": episode.code,
        "episode_code_match": code_matches,
        "series_alias_match": title_matches,
        "unmatched_series_prefix": unmatched_prefix,
    }
    evidence["episode_codes_found"] = [f"S{season:02d}E{number:02d}" for season, number in sorted(codes)]
    if len(codes) > 1 or re.search(r'S\d{1,2}E\d{1,3}\s*[-+]\s*E?\d{1,3}(?!\d)', candidate_title, re.I):
        evidence["reason"] = "multiple_episode_codes"
        return MatchTier.REJECT, evidence
    if code_matches and title_matches:
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

    def __init__(
        self,
        session: requests.Session,
        *,
        detector=None,
        request_gap_seconds: float = 2.0,
        discovery_timeout_seconds: float = 300,
        cache=None,
        request_gate=None,
        audio_lock=None,
    ):
        self.session = session
        self.detector = detector or PipelinedLanguageDetector()
        self.request_gap_seconds = request_gap_seconds
        self.discovery_timeout_seconds = discovery_timeout_seconds
        self._last_request = 0.0
        self.cache = cache
        self.request_gate = request_gate
        self.audio_lock = audio_lock or threading.Lock()
        self.last_outcome = None

    def fork_worker(self):
        """Independent cookies/connections, shared evidence and bounded audio CPU."""
        session = requests.Session()
        session.headers.update(self.session.headers)
        session.cookies.update(self.session.cookies)
        return type(self)(session, detector=self.detector, cache=self.cache,
                          request_gate=self.request_gate, audio_lock=self.audio_lock,
                          request_gap_seconds=self.request_gap_seconds,
                          discovery_timeout_seconds=self.discovery_timeout_seconds)

    @classmethod
    def authenticated(cls, email: str, password: str, **kwargs) -> "EpisodeSourceProvider":
        return cls(login_with_retry(login, email, password), **kwargs)

    def _get(self, url: str, *, session: requests.Session | None = None) -> requests.Response:
        active_session = session or self.session
        if session is None and self.request_gate is not None:
            self.request_gate.wait()
        elif session is None:
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
        deadline = time.monotonic() + self.discovery_timeout_seconds
        for title in series_aliases(episode):
            if not title:
                continue
            # Also search the series title alone: exact-code queries miss
            # release numbering aliases. Identity checks still apply to every
            # result, including the original detail title during inspection.
            for query in (f'{title} {episode.code}', title):
                url = f"{BASE_URL}/{slugify(query)}/s/-6"
                seen_pages = set()
                while url:
                    if url in seen_pages or len(seen_pages) >= 100 or time.monotonic() >= deadline:
                        raise SdilejError('Search pagination did not complete')
                    seen_pages.add(url)
                    page = self._search_page(url, query)
                    for row in page['candidates']:
                        candidate = Candidate.from_dict(row)
                        tier, evidence = episode_match(episode, candidate.title)
                        candidate.match_tier = tier
                        candidate.match_evidence = evidence
                        if tier in (MatchTier.STRONG, MatchTier.SOLID):
                            candidates.setdefault(candidate.source_id, candidate)
                    url = page['next']
                    if url and urlsplit(url).netloc != urlsplit(BASE_URL).netloc:
                        raise SdilejError('Unexpected search pagination host')
        return list(candidates.values())

    def _search_page(self, url, query):
        def fetch():
            html = self._get(url).text
            soup = BeautifulSoup(html, 'html.parser')
            next_page = soup.select_one('a[rel~="next"][href]')
            next_url = urljoin(url, next_page['href']) if next_page else None
            if next_url and not search_url(next_url):
                raise SdilejError('Unexpected search pagination address')
            fields = ('source_id', 'url', 'title', 'size_bytes', 'duration_sec', 'width', 'height')
            rows = [{key: getattr(candidate, key) for key in fields}
                    for candidate in parse_search_html(html, query=query)]
            return dict(candidates=rows, next=next_url)
        if self.cache is None:
            return fetch()
        # Parsed public search records only: never HTML, cookies or fast links.
        return self.cache.remember('search', url, fetch, ttl=6 * 3600,
            force=getattr(self, '_fresh_search', False),
            # An empty HTTP-200 page may be a temporary search outage. Never
            # preserve it for six hours or reuse old empty cache entries.
            cacheable=lambda page: bool(page['candidates'])
                and all(stable_url(r['url']) for r in page['candidates']))

    def discover_fresh(self, episode):
        """An unavailable queue source needs current listings, not a cached miss."""
        previous = getattr(self, '_fresh_search', False)
        self._fresh_search = True
        try:
            return self.discover(episode)
        finally:
            self._fresh_search = previous

    def _inspect(self, episode: Episode, candidate: Candidate) -> Candidate | None:
        detail = parse_detail_html(self._get(candidate.url).text, candidate)
        evidence = {}
        if self.request_gate is not None:
            self.request_gate.wait()
        detail = (resolve_original(self.session, detail, evidence=evidence) if self.cache is not None
                  else resolve_original(self.session, detail))
        reusable = True
        if evidence.pop('needs_content_fingerprint', False):
            evidence['content_fingerprint'] = sampled_content_fingerprint(self.session, detail)
            reusable = evidence['content_fingerprint'] is not None
        fingerprint = media_key(detail) + json.dumps(evidence, sort_keys=True) if reusable else None
        def inspect():
            media = probe_media(detail.download_url)
            return {k: media[k] for k in ('video_codec', 'width', 'height', 'duration_sec') if k in media}
        def complete(media):
            return (all(media.get(k) for k in ('width', 'height', 'duration_sec'))
                    or (all(k in media for k in ('video_codec', 'width', 'height'))
                        and not any(media[k] for k in ('video_codec', 'width', 'height'))))
        media = (self.cache.remember('media', fingerprint, inspect, cacheable=complete)
                 if self.cache is not None and fingerprint is not None else inspect())
        # A successful ffprobe with no selected video stream returns explicit
        # empty video fields (e.g. an AC3 file mislabeled .mkv). It cannot be an
        # episode candidate. An empty result is a probe failure and stays fatal
        # to selection; never downgrade around unresolved original metadata.
        video_fields = ('video_codec', 'width', 'height')
        if all(key in media for key in video_fields) and not any(media[key] for key in video_fields):
            return None
        if not all(media.get(key) for key in ('width', 'height', 'duration_sec')) or not detail.size_bytes:
            raise SdilejError('Original media metadata is incomplete')
        detail = replace(
            detail,
            video_codec=media.get("video_codec") or detail.video_codec,
            width=int(media.get("width") or detail.width),
            height=int(media.get("height") or detail.height),
            duration_sec=int(media.get("duration_sec") or detail.duration_sec or 0),
        )
        if not runtime_acceptable(episode, detail.duration_sec):
            return None
        tier, evidence = episode_match(episode, detail.title)
        if tier not in (MatchTier.STRONG, MatchTier.SOLID) or not quality_acceptable(detail):
            return None
        result = replace(detail, match_tier=tier, match_evidence=evidence)
        result._cache_fingerprint = fingerprint
        return result

    def _verify(self, episode: Episode, candidate: Candidate) -> Candidate | None:
        detail = self._inspect(episode, candidate)
        return self._verify_language(episode, detail) if detail else None

    def _verify_language(self, episode: Episode, detail: Candidate) -> Candidate:
        def detect():
            # The built-in detector downloads samples concurrently and locks
            # only model loading/inference. Custom detectors stay serialized.
            with (nullcontext() if getattr(self.detector, 'concurrent_samples', False)
                  else self.audio_lock):
                bounded_detect = getattr(self.detector, 'detect_for_duration', None)
                language, probability = (bounded_detect(detail.sample_url, detail.duration_sec)
                    if callable(bounded_detect) else self.detector.detect(detail.sample_url))
                hint = audio_language_hint(detail.filename)
                if probability < 0.65 or (hint and language_tier(language) != language_tier(hint)):
                    consensus = getattr(self.detector, "detect_consensus", None)
                    if consensus:
                        language, probability = consensus(detail.sample_url, detail.duration_sec, initial=(language, probability), preferred_language=hint)
                if probability < 0.65:
                    raise LanguageDetectionError("Whisper language confidence is too low")
                return dict(language=language, probability=probability)
        fingerprint = getattr(detail, '_cache_fingerprint', media_key(detail))
        version = 'v2-short' if detail.duration_sec and detail.duration_sec < 900 else 'v1'
        key = f'whisper-small-consensus-{version}:' + fingerprint if fingerprint is not None else None
        verified = self.cache.remember('audio', key, detect) if self.cache is not None and key is not None else detect()
        language, probability = verified['language'], verified['probability']
        return replace(
            detail,
            audio_language=language,
            language_probability=probability,
            language_evidence="whisper_remote_sample",
            language_tier=language_tier(language),
        )

    def discover(self, episode: Episode) -> Candidate | None:
        self.last_outcome = 'transient_search'
        deadline = time.monotonic() + self.discovery_timeout_seconds
        if time.monotonic() >= deadline:
            return None
        candidates = None
        for _attempt in range(2):
            if time.monotonic() >= deadline:
                return None
            try:
                previous = getattr(self, '_fresh_search', False)
                self._fresh_search = previous or _attempt > 0
                try:
                    candidates = self.search(episode)
                finally:
                    self._fresh_search = previous
                if candidates:
                    break
            except (SdilejError, requests.RequestException):
                candidates = None
                continue
        if candidates is None:
            # A transient search timeout must only defer this episode. Raising
            # here would terminate both producer workers and strand the upload
            # queue until the next scheduled Actions run.
            return None
        if time.monotonic() >= deadline:
            return None
        if not candidates:
            self.last_outcome = 'no_matches'
            return None
        return self._select_originals(episode, candidates, deadline)

    def revalidate_saved(self, episode: Episode, candidates: list[Candidate]) -> Candidate | None:
        """Recheck known HD originals without search; low-resolution sources need discovery."""
        return self._select_originals(episode, candidates,
                                      time.monotonic() + self.discovery_timeout_seconds,
                                      minimum_resolution=3)

    def _select_originals(self, episode, candidates, deadline, *, minimum_resolution=0):
        self.last_outcome = 'transient_original'
        by_resolution: dict[int, list[Candidate]] = {}
        for candidate in candidates:
            # Search metadata can describe a low-resolution preview of a 4K
            # original. Inspect every original before choosing a resolution.
            for attempt in range(2):
                if time.monotonic() >= deadline:
                    return None
                try:
                    detail = self._inspect(episode, candidate)
                    break
                except (SdilejError, requests.RequestException):
                    if attempt == 1:
                        return None
            if detail:
                by_resolution.setdefault(resolution_rank(detail.width, detail.height), []).append(detail)
        resolved: list[Candidate] = []
        self.last_outcome = 'inconclusive_audio'
        for resolution in sorted(by_resolution, reverse=True):
            if resolution < minimum_resolution:
                return None
            if time.monotonic() >= deadline:
                return None
            for candidate in sorted(
                by_resolution[resolution],
                key=lambda item: (
                    item.size_bytes is None or item.size_bytes <= 0,
                    item.size_bytes or 0,
                    item.source_id,
                ),
            ):
                if time.monotonic() >= deadline:
                    return None
                detail = None
                verification_completed = False
                for _attempt in range(2):
                    if time.monotonic() >= deadline:
                        return None
                    try:
                        detail = self._verify_language(episode, candidate)
                        verification_completed = True
                        break
                    except (SdilejError, LanguageDetectionError, requests.RequestException, subprocess.TimeoutExpired):
                        continue
                if not verification_completed:
                    # An unresolved better/smaller source is not evidence that
                    # a lower-quality/larger Czech source is the best choice.
                    return None
                if detail:
                    resolved.append(detail)
                    # Candidates in this resolution tier are ordered by size.
                    # All originals were inspected first, and every preceding
                    # candidate was conclusively checked before reaching here.
                    if detail.language_tier == LanguageTier.CZECH_AUDIO:
                        self.last_outcome = 'verified_czech'
                        return rank_candidates(resolved)[0]
        ranked = rank_candidates(resolved)
        self.last_outcome = 'verified_non_czech' if ranked else 'no_acceptable_original'
        return ranked[0] if ranked else None

    def refresh(self, candidate: Candidate, *, session: requests.Session) -> Candidate:
        return parse_detail_html(self._get(candidate.url, session=session).text, candidate)
