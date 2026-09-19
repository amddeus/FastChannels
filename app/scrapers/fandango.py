from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import unquote

from .base import BaseScraper, ChannelData, ProgramData

logger = logging.getLogger(__name__)


@dataclass
class _FandangoCandidate:
    channel_id: str
    name: str
    stream_url: str


class FandangoScraper(BaseScraper):
    """
    Scraper for free live Bundesliga streams from Fandango.

    Fandango's CDN stream URLs are short-lived, so this scraper stores an
    opaque per-channel URI and resolves to a fresh HLS URL at playback time.
    """

    source_name = 'fandango'
    display_name = 'Fandango Bundesliga'
    scrape_interval = 120
    stream_audit_enabled = True
    epg_quality = 'basic'
    source_category = 'specialty'

    config_schema = []

    _HOME_URL = 'https://home.fandango.com'
    _BUNDESLIGA_KEYWORDS = (
        'bundesliga',
        'bundes liga',
        'dfb',
        'german league',
    )

    _URL_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?', re.IGNORECASE)
    _CHANNEL_ID_RE = re.compile(r'channel\(([^)]+)\)', re.IGNORECASE)
    _TITLE_RE = re.compile(
        r'"(?:title|name|eventName|matchTitle|programName|label)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
        re.IGNORECASE,
    )
    _SCRIPT_JSON_RE = re.compile(r'<script[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)
    _NEXT_DATA_RE = re.compile(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    _WINDOW_JSON_RE = re.compile(
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/126.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': self._HOME_URL + '/',
            'Origin': self._HOME_URL,
        })

    def fetch_channels(self) -> list[ChannelData]:
        candidates = self._discover_candidates()
        if not candidates:
            logger.info('[%s] no live Bundesliga channels found', self.source_name)
            return []

        channels: list[ChannelData] = []
        for idx, c in enumerate(candidates, start=1):
            channel_id = c.channel_id or f'bundesliga-{idx}'
            channels.append(ChannelData(
                source_channel_id=channel_id,
                name=c.name or f'Bundesliga Live {idx}',
                stream_url=f'fandango://{channel_id}',
                category='Sports',
                country='DE',
                language='de',
                stream_type='hls',
            ))

        logger.info('[%s] %d live Bundesliga channel(s)', self.source_name, len(channels))
        return channels

    def fetch_epg(self, channels: list[ChannelData], **kwargs) -> list[ProgramData]:
        # Fandango does not expose a stable public EPG payload.
        return []

    def resolve(self, raw_url: str) -> str:
        if not raw_url.startswith('fandango://'):
            return raw_url

        wanted = raw_url[len('fandango://'):].strip()
        candidates = self._discover_candidates()
        if not candidates:
            logger.warning('[%s] no live Bundesliga stream found during resolve for %s', self.source_name, wanted)
            return raw_url

        by_id = {c.channel_id: c.stream_url for c in candidates if c.channel_id}
        if wanted in by_id:
            return by_id[wanted]

        # Legacy/fallback: if a row somehow stored a direct channel(...) id.
        if wanted.startswith('channel(') and wanted.endswith(')'):
            plain = wanted[8:-1]
            if plain in by_id:
                return by_id[plain]

        if len(candidates) == 1:
            return candidates[0].stream_url

        logger.warning('[%s] unresolved channel id %s; using first live stream', self.source_name, wanted)
        return candidates[0].stream_url

    def _discover_candidates(self) -> list[_FandangoCandidate]:
        html = self._fetch_homepage()
        if not html:
            return []

        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        candidates: list[_FandangoCandidate] = []

        def _add(url: str, context_text: str = '', preferred_name: str | None = None):
            cleaned = self._clean_url(url)
            if not cleaned or '.m3u8' not in cleaned.lower():
                return
            if 'vos360.video' not in cleaned.lower():
                return

            combined_context = f'{context_text} {preferred_name or ""}'.strip()
            if not self._looks_like_bundesliga(combined_context):
                return

            if cleaned in seen_urls:
                return
            seen_urls.add(cleaned)

            channel_id = self._extract_channel_id(cleaned)
            if channel_id and channel_id in seen_ids:
                return
            if channel_id:
                seen_ids.add(channel_id)

            name = (preferred_name or self._extract_title_from_text(context_text) or '').strip()
            if not name:
                name = f'Bundesliga Live {channel_id}' if channel_id else 'Bundesliga Live'

            candidates.append(_FandangoCandidate(
                channel_id=channel_id or f'auto-{len(candidates) + 1}',
                name=name,
                stream_url=cleaned,
            ))

        # 1) Direct extraction from HTML around URL context.
        normalized = html.replace('\\u002F', '/').replace('\\/', '/')
        for m in self._URL_RE.finditer(normalized):
            url = m.group(0)
            start = max(0, m.start() - 800)
            end = min(len(normalized), m.end() + 800)
            snippet = normalized[start:end]
            _add(url, context_text=snippet)

        # 2) Structured extraction from embedded JSON payloads.
        for payload in self._extract_json_payloads(html):
            for node, context in self._walk_nodes(payload):
                if isinstance(node, str):
                    for m in self._URL_RE.finditer(node.replace('\\u002F', '/').replace('\\/', '/')):
                        _add(m.group(0), context_text=context)
                elif isinstance(node, dict):
                    direct_name = self._extract_title_from_mapping(node)
                    for value in node.values():
                        if isinstance(value, str):
                            txt = value.replace('\\u002F', '/').replace('\\/', '/')
                            for m in self._URL_RE.finditer(txt):
                                _add(m.group(0), context_text=context, preferred_name=direct_name)

        return candidates

    def _fetch_homepage(self) -> str | None:
        r = self.get(self._HOME_URL)
        return r.text if r else None

    def _extract_json_payloads(self, html: str) -> list[object]:
        payloads: list[object] = []

        for m in self._NEXT_DATA_RE.finditer(html):
            obj = self._safe_json_loads(m.group(1))
            if obj is not None:
                payloads.append(obj)

        for m in self._WINDOW_JSON_RE.finditer(html):
            obj = self._safe_json_loads(m.group(1))
            if obj is not None:
                payloads.append(obj)

        for m in self._SCRIPT_JSON_RE.finditer(html):
            block = (m.group(1) or '').strip()
            if not block:
                continue
            lowered = block.lower()
            if 'bundesliga' not in lowered and 'vos360' not in lowered and 'm3u8' not in lowered:
                continue
            obj = self._safe_json_loads(block)
            if obj is not None:
                payloads.append(obj)

        return payloads

    def _walk_nodes(self, node, context: str = ''):
        if isinstance(node, dict):
            local = self._context_from_mapping(node)
            combined = ' '.join(part for part in (context, local) if part).strip()
            yield node, combined
            for value in node.values():
                yield from self._walk_nodes(value, combined)
            return

        if isinstance(node, list):
            for item in node:
                yield from self._walk_nodes(item, context)
            return

        yield node, context

    @staticmethod
    def _safe_json_loads(raw: str):
        text = (raw or '').strip()
        if not text:
            return None

        if text.startswith('<!--'):
            text = text.strip('<!-> ').strip()

        try:
            return json.loads(text)
        except Exception:
            try:
                return json.loads(unescape(text))
            except Exception:
                return None

    @classmethod
    def _context_from_mapping(cls, mapping: dict) -> str:
        pieces: list[str] = []
        keys = (
            'title', 'name', 'eventName', 'matchTitle', 'competition', 'league',
            'tournament', 'description', 'sport', 'category', 'subtitle',
            'shortDescription',
        )
        for key in keys:
            val = mapping.get(key)
            if isinstance(val, str) and val.strip():
                pieces.append(val.strip())
        return ' | '.join(pieces)

    @classmethod
    def _extract_title_from_mapping(cls, mapping: dict) -> str | None:
        for key in ('title', 'name', 'eventName', 'matchTitle', 'subtitle'):
            val = mapping.get(key)
            if isinstance(val, str) and val.strip() and cls._looks_like_bundesliga(val):
                return val.strip()
        for key in ('title', 'name', 'eventName', 'matchTitle', 'subtitle'):
            val = mapping.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    @classmethod
    def _extract_title_from_text(cls, text: str) -> str | None:
        for m in cls._TITLE_RE.finditer(text or ''):
            val = cls._decode_string(m.group(1)).strip()
            if not val:
                continue
            if cls._looks_like_bundesliga(val):
                return val
        for m in cls._TITLE_RE.finditer(text or ''):
            val = cls._decode_string(m.group(1)).strip()
            if val:
                return val
        return None

    @classmethod
    def _extract_channel_id(cls, url: str) -> str | None:
        m = cls._CHANNEL_ID_RE.search(url or '')
        if not m:
            return None
        return m.group(1).strip() or None

    @staticmethod
    def _decode_string(val: str) -> str:
        out = unescape(val or '')
        out = out.replace('\\/', '/').replace('\\u002F', '/')
        try:
            out = bytes(out, 'utf-8').decode('unicode_escape')
        except Exception:
            pass
        return out

    @classmethod
    def _clean_url(cls, val: str) -> str:
        out = cls._decode_string((val or '').strip().strip("'\""))
        if not out:
            return ''

        # Handle URL-encoded strings occasionally embedded in JSON values.
        if out.lower().startswith('http%3a') or out.lower().startswith('https%3a'):
            out = unquote(out)

        out = out.replace(' ', '%20')
        while out and out[-1] in '.,;)]}':
            out = out[:-1]
        return out

    @classmethod
    def _looks_like_bundesliga(cls, text: str) -> bool:
        lowered = (text or '').lower()
        return any(k in lowered for k in cls._BUNDESLIGA_KEYWORDS)
