"""SSFlix catalogue and browser-assisted public player discovery."""
import base64
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urljoin, urlparse

import requests
import yt_dlp

AUDIO_LANGUAGES = {
    "hi": ("Hindi", "hi|hin|hindi"),
    "ur": ("Urdu", "ur|urd|urdu"),
    "en": ("English", "en|eng|english"),
}


def audio_language_filter(language):
    if language not in AUDIO_LANGUAGES:
        raise SSFlixError('Unsupported audio language.')
    return f"[language~='(?i)^({AUDIO_LANGUAGES[language][1]})([-_].*)?$']"


def has_audio_language(media, headers, language):
    # Fail closed: an unknown/unlabelled track is not confirmation of a Hindi dub.
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "socket_timeout": 12, "retries": 0, "http_headers": headers,
                               "format": "bestaudio" + audio_language_filter(language) + "/best" + audio_language_filter(language),
                               "logger": _ProbeLogger()}) as ydl:
            ydl.extract_info(media, download=False)
        return True
    except yt_dlp.utils.DownloadError:
        return False


class _ProbeLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


class SSFlixError(ValueError):
    pass


@dataclass(frozen=True)
class Title:
    kind: str
    slug: str
    id: str
    season: int | None
    episode: int | None

    def watch_url(self, season=None, episode=None):
        base = f"https://ssflix.pro/watch/{self.kind}/{self.slug}"
        if self.kind == "serie":
            base += f"/season-{season if season is not None else self.season or 1}/episode-{episode if episode is not None else self.episode or 1}"
        return base


def is_ssflix(url):
    return (urlparse(url).hostname or '').lower() in {'ssflix.pro', 'www.ssflix.pro'}


def parse_title(url):
    if not is_ssflix(url):
        raise SSFlixError('Use a movie or series link from ssflix.pro.')
    p = urlparse(url)
    m = re.fullmatch(r'/(?:watch/)?(movie|serie)/([\w-]+?)(?:/season-(\d+))?(?:/episode-(\d+))?/?', p.path)
    if not m:
        raise SSFlixError('Use an SSFlix movie, series, season or episode link.')
    kind, slug, season, episode = m.groups()
    ident = re.search(r'(?:^|-)(\d+)$', slug)
    if not ident:
        raise SSFlixError('The SSFlix link is missing its title ID.')
    return Title(kind, slug, ident[1], int(season) if season else None, int(episode) if episode else None)


def get_text(url):
    try:
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        raise SSFlixError(f'Could not reach SSFlix: {e}') from e


@lru_cache(maxsize=1)
def catalog_key():
    # Read the same public catalogue configuration used by the site; never store its key.
    html = get_text('https://ssflix.pro/watch/movie/550')
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)', html)
    for src in scripts:
        target = urljoin('https://ssflix.pro', src)
        if urlparse(target).hostname != 'ssflix.pro':
            continue
        script = get_text(target)
        m = re.search(r'ADS_KEY\s*:\s*atob\(["\']([A-Za-z0-9+/=]+)', script)
        if m:
            return base64.b64decode(m[1]).decode()
    raise SSFlixError('SSFlix changed its catalogue configuration. No episodes were guessed.')


def catalog_get(path):
    try:
        r = requests.get(f'https://api.themoviedb.org/3/{path}',
                         params={'api_key': catalog_key(), 'language': 'en'}, timeout=25)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise SSFlixError(f'Could not load the SSFlix catalogue: {e}') from e


def catalog(url):
    title = parse_title(url)
    data = catalog_get(f'{"tv" if title.kind == "serie" else "movie"}/{title.id}')
    name = data.get('name') or data.get('title') or title.slug
    poster = data.get('poster_path')
    thumbnail = f'https://image.tmdb.org/t/p/w500{poster}' if poster else None
    videos = []
    seasons = []
    if title.kind == 'movie':
        videos.append({'url': title.watch_url(), 'title': name, 'thumbnail': thumbnail,
                       'duration': (data.get('runtime') or 0) * 60, 'season': None, 'episode': None})
    else:
        seasons = [{'number': s['season_number'], 'name': s.get('name') or f"Season {s['season_number']}",
                    'count': s.get('episode_count', 0)} for s in data.get('seasons', [])
                   if s['season_number'] > 0 and s.get('episode_count')]
        # Read every real season, so a pasted episode can also be expanded to the whole series.
        for s in seasons:
            season = catalog_get(f'tv/{title.id}/season/{s["number"]}')
            for e in season.get('episodes', []):
                n = e['episode_number']
                still = e.get('still_path')
                videos.append({'url': title.watch_url(s['number'], n),
                               'title': f'{name} S{s["number"]:02d}E{n:02d} - {e.get("name") or "Episode " + str(n)}',
                               'season': s['number'], 'episode': n,
                               'duration': (e.get('runtime') or 0) * 60,
                               'thumbnail': f'https://image.tmdb.org/t/p/w500{still}' if still else thumbnail})
    if not videos:
        raise SSFlixError('No movies or episodes found for this title.')
    return {'title': name, 'site': 'SSFlix', 'kind': title.kind, 'count': len(videos), 'videos': videos,
            'seasons': seasons, 'selected_season': title.season, 'selected_episode': title.episode}


def selected_entry(url):
    title = parse_title(url)
    data = catalog_get(f'{"tv" if title.kind == "serie" else "movie"}/{title.id}')
    name = data.get('name') or data.get('title') or title.slug
    if title.kind == 'serie':
        season, episode = title.season or 1, title.episode or 1
        e = catalog_get(f'tv/{title.id}/season/{season}/episode/{episode}')
        name = f'{name} S{season:02d}E{episode:02d} - {e.get("name") or "Episode " + str(episode)}'
        data.update(e)
    poster = data.get('still_path') or data.get('poster_path')
    return {'title': name, 'thumbnail': f'https://image.tmdb.org/t/p/w500{poster}' if poster else None,
            'duration': (data.get('runtime') or 0) * 60, 'uploader': None, 'site': 'SSFlix',
            'heights': [], 'max_height': None}


def select_labelled_stream(streams, language, base_url):
    """Some providers publish each dub as a separate, explicitly named stream."""
    if language not in AUDIO_LANGUAGES:
        return None
    names = AUDIO_LANGUAGES[language][1].split('|')
    for stream in streams:
        label = str(stream.get('name') or '').split(' · ')[-1].strip().lower()
        if label not in names:
            continue
        media_url = urljoin(base_url, stream.get('url') or '')
        if stream.get('url') and urlparse(media_url).scheme in {'https', 'http'}:
            return {'url': media_url, 'audio_language': language, 'source_label': stream['name']}
    return None


def labelled_player_media(page, language):
    if language == "auto":
        return None
    iframe = page.query_selector('#player-iframe')
    frame = iframe.content_frame() if iframe else None
    if not frame or (urlparse(frame.url).hostname or '') not in {'embed.vidrift.net', 'vidrift.net'}:
        return None
    streams = frame.evaluate("typeof embedMeta !== 'undefined' ? (embedMeta.orionStreams || []) : []")
    media = select_labelled_stream(streams, language, frame.url)
    if media:
        origin = urlparse(frame.url)
        media['headers'] = {'Referer': frame.url, 'Origin': f'{origin.scheme}://{origin.netloc}',
                            'User-Agent': page.evaluate('navigator.userAgent')}
        cookies = page.context.cookies(media['url'])
        if cookies:
            media['headers']['Cookie'] = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
    return media


def resolve_media(url, audio_language="auto", server=None):
    """Observe playable media from the site's own player, trying its alternate servers."""
    title = parse_title(url)
    if server is not None and (type(server) is not int or server not in range(1, 7)):
        raise SSFlixError('Choose an SSFlix server from 1 to 6.')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SSFlixError('Install player support: ./venv/bin/pip install playwright && ./venv/bin/python -m playwright install chromium') from e
    candidates = []
    seen = set()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                def capture(response):
                    path = urlparse(response.url).path.lower()
                    mime = response.headers.get('content-type', '').lower()
                    if response.status < 400 and (path.endswith(('.m3u8', '.mpd', '.mp4', '.webm')) or
                                                  any(t in mime for t in ('mpegurl', 'dash+xml', 'video/mp4'))):
                        # Only accept media from the player iframe and its descendants.
                        # Tracking/advertising frames elsewhere on the page are excluded.
                        try:
                            frame = response.request.frame
                            while frame.parent_frame and frame.parent_frame != page.main_frame:
                                frame = frame.parent_frame
                            if frame.parent_frame and frame.frame_element().get_attribute('id') == 'player-iframe':
                                candidates.append((response.url, response.request.all_headers()))
                        except Exception:
                            pass
                page.on('response', capture)
                page.goto(title.watch_url(), wait_until='domcontentloaded', timeout=45000)
                # Popups are advertising, never a source for downloads.
                context.on('page', lambda popup: popup.close())
                page.wait_for_function('typeof window.switchServer === "function"', timeout=20000)
                selected_server = server
                for server_number in ([selected_server] if selected_server else range(1, 7)):
                    page.evaluate('(n) => window.switchServer(n)', server_number)
                    candidates.clear()
                    # Some embedded players need longer than 15 seconds to load providers.
                    for tick in range(90 if selected_server else 25):
                        page.wait_for_timeout(1000)
                        try:
                            labelled = labelled_player_media(page, audio_language)
                        except Exception:
                            labelled = None  # iframe may be in the middle of navigation
                        if labelled:
                            return {**labelled, 'server': server_number}
                        if candidates:
                            media, headers = candidates.pop(0)
                            if media in seen:
                                continue
                            seen.add(media)
                            headers = {k: v for k, v in headers.items()
                                       if k.lower() in {'referer', 'origin', 'user-agent', 'cookie', 'authorization'}}
                            if audio_language == "auto" or has_audio_language(media, headers, audio_language):
                                return {'url': media, 'headers': headers, 'server': server_number}
                            # Wait for labelled dubs too: this player may expose them after its default stream.
                            if not selected_server and server_number != 5:
                                break
                        if tick in (2, 6, 10) or tick % 10 == 0:
                            for frame in page.frames[1:]:
                                try:
                                    frame.locator('video, .vjs-big-play-button, .jw-icon-playback, button[aria-label*="Play"]').first.click(timeout=400)
                                except Exception:
                                    pass
                source = f'SSFlix Server {selected_server}' if selected_server else 'the SSFlix servers'
                if audio_language != "auto":
                    raise SSFlixError(f'No confirmed {AUDIO_LANGUAGES[audio_language][0]} audio track found on {source}. A dubbed source with a labelled audio track is required; English was not downloaded.')
                raise SSFlixError(f'No playable media found on {source}. The player may be unavailable or require interaction. Try again later.')
            finally:
                browser.close()
    except SSFlixError:
        raise
    except Exception as e:
        raise SSFlixError(f'Could not open the SSFlix player. Install Chromium with ./venv/bin/python -m playwright install chromium. {str(e)[:180]}') from e
