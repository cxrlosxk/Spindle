"""Public Apple iTunes catalog search, with cautious scoring and exact-link lookup.

Note: catalog lookup does not imply a song is playable in the user's subscription.
"""
import json
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen, Request as UrlRequest
from .models import Track


class CatalogError(RuntimeError):
    pass


def norm(text: str) -> str:
    s = ''.join(c for c in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-z0-9]+', s))


def track_from_api(item: dict) -> Track:
    return Track(title=item.get('trackName', '').strip(), artist=item.get('artistName', '').strip(),
                 album=item.get('collectionName', '').strip(), duration_ms=int(item.get('trackTimeMillis') or 0),
                 track_id=str(item.get('trackId') or ''), url=item.get('trackViewUrl') or '')


def is_song_url(text: str) -> bool:
    parsed = urlparse(text.strip())
    return parsed.scheme == 'https' and parsed.hostname in ('music.apple.com', 'itunes.apple.com')


def apple_song_id(url: str) -> str | None:
    if not is_song_url(url):
        return None
    parsed = urlparse(url)
    qp = parse_qs(parsed.query)
    if qp.get('i') and re.fullmatch(r'\d{5,20}', qp['i'][0]):
        return qp['i'][0]
    if '/song/' in parsed.path:
        tail = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        if re.fullmatch(r'\d{5,20}', tail):
            return tail
    return None  # Don't accidentally queue an album ID.


def relevance(query: str, track: Track) -> float:
    q = norm(query)
    title, artist = norm(track.title), norm(track.artist)
    qt = set(q.split())
    all_tokens = set((title + ' ' + artist).split())
    coverage = len(qt & all_tokens) / max(len(qt), 1)
    title_fit = max(SequenceMatcher(None, q, title).ratio(),
                    SequenceMatcher(None, q, title + ' ' + artist).ratio(),
                    SequenceMatcher(None, q, artist + ' ' + title).ratio())
    score = .67 * coverage + .33 * title_fit
    if qt and set(title.split()).issubset(qt):
        score += .12
    if '(live)' in track.title.lower() and 'live' not in qt:
        score -= .22
    if 'remix' in track.title.lower() and 'remix' not in qt:
        score -= .22
    if 'karaoke' in track.title.lower() and 'karaoke' not in qt:
        score -= .25
    return score


class Catalog:
    def __init__(self, country='US', fetch=None):
        self.country = country
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(url: str) -> dict:
        req = UrlRequest(url, headers={'User-Agent': 'Spindle/0.1 (Apple Music request manager)'})
        try:
            with urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as exc:
            raise CatalogError('Apple catalog search is unavailable. Try again shortly.') from exc

    def search(self, query: str) -> list[Track]:
        query = query.strip()[:160]
        if not query:
            raise CatalogError('Use !sr followed by a song and artist.')
        if is_song_url(query):
            song_id = apple_song_id(query)
            if not song_id:
                raise CatalogError('Please share a direct Apple Music song link, not an album or playlist link.')
            url = 'https://itunes.apple.com/lookup?' + urlencode({'id': song_id, 'entity': 'song', 'country': self.country})
        else:
            url = 'https://itunes.apple.com/search?' + urlencode({'term': query, 'media': 'music', 'entity': 'song',
                                                                   'country': self.country, 'limit': 35})
        payload = self.fetch(url)
        found = [track_from_api(r) for r in payload.get('results', []) if r.get('kind') == 'song' and r.get('trackId')]
        if is_song_url(query):
            found = [t for t in found if t.track_id == apple_song_id(query)]
        else:
            found.sort(key=lambda t: relevance(query, t), reverse=True)
        return found

    def best(self, query: str) -> Track:
        tracks = self.search(query)
        if not tracks:
            raise CatalogError('I could not find that song. Try including the artist or pasting a song link.')
        if is_song_url(query):
            return tracks[0]
        top = tracks[0]
        best_score = relevance(query, top)
        if best_score < .73:
            raise CatalogError('I could not confidently match that song. Add the artist or use an Apple Music song link.')
        if len(tracks) > 1:
            second = tracks[1]
            if relevance(query, second) >= best_score - .035 and (
                norm(second.title) != norm(top.title) or norm(second.artist) != norm(top.artist)
            ):
                raise CatalogError('Several songs matched. Please include the artist and title or use a direct song link.')
        return top
