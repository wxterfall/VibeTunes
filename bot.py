from __future__ import annotations

import asyncio
import bisect
import json
import logging
import os
import random
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from html import unescape
from typing import Any, Deque
from urllib.parse import parse_qs, urlparse

import discord
import lyricsgenius
import requests
import spotipy
import yt_dlp
from discord.ext import commands
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyClientCredentials

try:
    import syncedlyrics
except ImportError:
    syncedlyrics = None


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!").strip() or "!"
MAX_SPOTIFY_TRACKS = int(os.getenv("MAX_SPOTIFY_TRACKS", "200"))
BOT_NAME = os.getenv("BOT_NAME", "VibeTunes").strip() or "VibeTunes"
QUEUE_PAGE_SIZE = 10
NOW_PLAYING_UPDATE_SECONDS = int(os.getenv("NOW_PLAYING_UPDATE_SECONDS", "1"))
IDLE_DISCONNECT_SECONDS = int(os.getenv("IDLE_DISCONNECT_SECONDS", "600"))
SYNCED_LYRICS_ENABLED = os.getenv("SYNCED_LYRICS_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
SYNCED_LYRICS_PROVIDERS = [
    provider.strip()
    for provider in os.getenv("SYNCED_LYRICS_PROVIDERS", "").split(",")
    if provider.strip()
]
SYNCED_LYRICS_CONTEXT_LINES = max(
    0,
    int(os.getenv("SYNCED_LYRICS_CONTEXT_LINES", "1")),
)
SYNCED_LYRICS_OFFSET_SECONDS = float(os.getenv("SYNCED_LYRICS_OFFSET_SECONDS", "0"))


def env_color(name: str, fallback: str) -> int:
    value = os.getenv(name, fallback).strip().lstrip("#")
    try:
        return int(value, 16)
    except ValueError:
        return int(fallback.lstrip("#"), 16)


PRIMARY_COLOR = env_color("EMBED_COLOR", "C026D3")
SUCCESS_COLOR = env_color("SUCCESS_COLOR", "57F287")
ERROR_COLOR = env_color("ERROR_COLOR", "ED4245")
WARNING_COLOR = env_color("WARNING_COLOR", "FEE75C")

FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn"

YTDL_OPTIONS: dict[str, Any] = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "no_color": True,
    "socket_timeout": 15,
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios"],
        }
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("simple_music_bot")


def build_spotify_client() -> spotipy.Spotify | None:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        logger.warning("Spotify credentials are missing; Spotify links are disabled.")
        return None

    try:
        return spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
            )
        )
    except Exception:
        logger.exception("Could not initialize Spotify client.")
        return None


def build_genius_client() -> lyricsgenius.Genius | None:
    token = os.getenv("GENIUS_TOKEN", "").strip()
    if not token:
        logger.warning("GENIUS_TOKEN is missing; lyrics are disabled.")
        return None

    try:
        genius = lyricsgenius.Genius(
            token,
            remove_section_headers=True,
            skip_non_songs=True,
            excluded_terms=["(Remix)", "(Live)"],
        )
        genius.verbose = False
        return genius
    except Exception:
        logger.exception("Could not initialize Genius client.")
        return None


spotify_client = build_spotify_client()
genius_client = build_genius_client()


intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or(COMMAND_PREFIX),
    intents=intents,
    help_command=None,
)


@dataclass
class QueueItem:
    query: str
    title: str
    requester: str
    requester_avatar_url: str | None = None
    artist: str | None = None
    webpage_url: str | None = None
    info: dict[str, Any] | None = None


@dataclass
class LyricLine:
    timestamp: float
    text: str


@dataclass
class SyncedLyrics:
    query: str
    lines: list[LyricLine]


@dataclass
class Track:
    title: str
    stream_url: str
    webpage_url: str | None
    duration: int | None
    requester: str
    source_item: QueueItem
    requester_avatar_url: str | None = None
    artist: str | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    synced_lyrics: SyncedLyrics | None = None
    synced_lyrics_status: str | None = None


class GuildPlayer:
    def __init__(self) -> None:
        self.queue: Deque[QueueItem] = deque()
        self.current: Track | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.lock = asyncio.Lock()
        self.playback_active = False
        self.manual_stop = False
        self.skip_requested = False
        self.loop_current = False
        self.volume = 0.5
        self.now_playing_message: discord.Message | None = None
        self.now_playing_messages: list[discord.Message] = []
        self.progress_task: asyncio.Task | None = None
        self.synced_lyrics_message: discord.Message | None = None
        self.synced_lyrics_task: asyncio.Task | None = None
        self.idle_disconnect_task: asyncio.Task | None = None
        self.playback_started_at: float | None = None
        self.paused_started_at: float | None = None
        self.total_paused_seconds = 0.0


players: dict[int, GuildPlayer] = {}


def player_for(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_spotify_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return "spotify.com" in host or host == "spotify.link"


def is_soundcloud_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return "soundcloud.com" in host or host == "on.soundcloud.com"


def should_flat_extract_url(value: str) -> bool:
    if not is_url(value):
        return False
    if is_soundcloud_url(value):
        return False
    return True


def is_deezer_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return "deezer.com" in host or host == "deezer.page.link"


def is_apple_music_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return host == "music.apple.com" or host.endswith(".music.apple.com")


def is_tidal_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return host == "tidal.com" or host.endswith(".tidal.com")


def is_amazon_music_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return host == "music.amazon.com" or (
        host.startswith("music.amazon.") and len(host) > len("music.amazon.")
    )


def is_metadata_resolver_url(value: str) -> bool:
    return (
        is_deezer_url(value)
        or is_apple_music_url(value)
        or is_tidal_url(value)
        or is_amazon_music_url(value)
    )


def sanitize_query(query: str) -> str:
    query = re.sub(r"[\x00-\x1F\x7F]", "", query)
    return re.sub(r"\s+", " ", query).strip()


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


def metadata_to_queue_item(
    *,
    title: str,
    artist: str | None,
    requester: str,
    requester_avatar_url: str | None,
    webpage_url: str | None,
    duration: int | float | None = None,
    thumbnail: str | None = None,
) -> QueueItem | None:
    title = sanitize_query(title)
    artist = sanitize_query(artist or "")
    if not title:
        return None

    search_text = sanitize_query(f"{title} {artist}".strip())
    info = {
        key: value
        for key, value in {
            "duration": int(duration) if duration is not None else None,
            "thumbnail": thumbnail,
        }.items()
        if value is not None
    }
    return QueueItem(
        query=f"ytsearch1:{search_text}",
        title=title,
        requester=requester,
        requester_avatar_url=requester_avatar_url,
        artist=artist or None,
        webpage_url=webpage_url,
        info=info or None,
    )


def parse_duration_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)

    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        return None

    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def http_get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(
        url,
        params=params,
        headers=REQUEST_HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def http_get_response(url: str) -> requests.Response:
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=15,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def http_get_text(url: str) -> str:
    return http_get_response(url).text


def first_meta_content(html: str, *keys: str) -> str | None:
    wanted = {key.lower() for key in keys}
    for tag in re.findall(r"<meta\b[^>]*>", html, flags=re.IGNORECASE):
        attrs = {
            name.lower(): unescape(value.strip())
            for name, value in re.findall(
                r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']',
                tag,
                flags=re.IGNORECASE,
            )
        }
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if key in wanted and content:
            return content
    return None


def first_html_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.S)
    if not match:
        return None
    return sanitize_query(unescape(re.sub(r"\s+", " ", match.group(1))))


def iter_json_ld(html: str) -> list[Any]:
    values: list[Any] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.S,
    ):
        raw_json = unescape(match.group(1)).strip()
        if not raw_json:
            continue
        try:
            values.append(json.loads(raw_json))
        except json.JSONDecodeError:
            continue
    return values


def flatten_json_ld(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        items = [value]
        graph = value.get("@graph")
        if isinstance(graph, list):
            for entry in graph:
                items.extend(flatten_json_ld(entry))
        return items
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for entry in value:
            items.extend(flatten_json_ld(entry))
        return items
    return []


def json_ld_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "alternateName"):
            if value.get(key):
                return str(value[key])
    if isinstance(value, list) and value:
        return json_ld_text(value[0])
    return None


def clean_provider_title(title: str, provider: str) -> str:
    title = sanitize_query(title)
    title = re.sub(
        rf"\s*(?:\||-|on)\s*{re.escape(provider)}(?:\s+Music)?\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*\|\s*Amazon Music\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*-\s*Amazon Music\s*$", "", title, flags=re.IGNORECASE)
    return sanitize_query(title)


def split_title_artist_from_page(title: str, provider: str) -> tuple[str, str | None]:
    title = clean_provider_title(title, provider)
    if " by " in title:
        song, artist = title.split(" by ", 1)
        if song.strip() and artist.strip():
            return sanitize_query(song), sanitize_query(artist)
    return title, None


def duration_text(seconds: int | None) -> str:
    if seconds is None:
        return "live/unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def split_artist_title(title: str) -> tuple[str, str | None]:
    if " - " in title:
        artist, song = title.split(" - ", 1)
        if artist.strip() and song.strip():
            return song.strip(), artist.strip()
    if " by " in title:
        song, artist = title.split(" by ", 1)
        if song.strip() and artist.strip():
            return song.strip(), artist.strip()
    return title.strip(), None


def normalize_artist_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def display_song_artist(title: str, artist: str | None = None) -> tuple[str, str | None]:
    title = title.strip()
    artist = artist.strip() if artist else None
    if not artist:
        return split_artist_title(title)

    if " - " in title:
        possible_artist, possible_song = title.split(" - ", 1)
        normalized_possible = normalize_artist_match(possible_artist)
        normalized_artist = normalize_artist_match(artist)
        if normalized_possible and (
            normalized_possible in normalized_artist
            or normalized_artist in normalized_possible
        ):
            return possible_song.strip(), artist

    return title, artist


LRC_TIMESTAMP_RE = re.compile(
    r"\[(?P<minutes>\d{1,3}):(?P<seconds>\d{2})(?:[.:](?P<fraction>\d{1,3}))?\]"
)


def clean_lyrics_search_part(value: str) -> str:
    value = re.sub(r"\[[^\]]+\]|\([^)]+\)", " ", value)
    value = re.sub(
        r"\b(official|audio|video|lyrics?|lyric video|visualizer|remaster(?:ed)?)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return sanitize_query(value)


def synced_lyrics_query(track: Track) -> str:
    title, artist = display_song_artist(
        track.source_item.title or track.title,
        track.artist or track.source_item.artist,
    )
    title = clean_lyrics_search_part(title)
    artist = clean_lyrics_search_part(artist or "")

    if artist:
        return f"{title} {artist}".strip()
    if track.uploader:
        uploader = clean_lyrics_search_part(track.uploader)
        if uploader and normalize_artist_match(uploader) not in normalize_artist_match(title):
            return f"{title} {uploader}".strip()
    return title


def lrc_timestamp_seconds(match: re.Match[str]) -> float:
    fraction = match.group("fraction") or ""
    fraction_seconds = int(fraction) / (10 ** len(fraction)) if fraction else 0
    return (
        int(match.group("minutes")) * 60
        + int(match.group("seconds"))
        + fraction_seconds
    )


def parse_lrc(lrc: str) -> list[LyricLine]:
    parsed: list[LyricLine] = []

    for raw_line in lrc.splitlines():
        matches = list(LRC_TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue

        text = LRC_TIMESTAMP_RE.sub("", raw_line).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue

        for match in matches:
            parsed.append(LyricLine(lrc_timestamp_seconds(match), text))

    parsed.sort(key=lambda line: line.timestamp)

    deduped: list[LyricLine] = []
    for line in parsed:
        if (
            deduped
            and abs(deduped[-1].timestamp - line.timestamp) < 0.01
            and deduped[-1].text == line.text
        ):
            continue
        deduped.append(line)
    return deduped


def search_synced_lyrics_for_query_sync(query: str) -> SyncedLyrics | None:
    if syncedlyrics is None:
        return None

    query = sanitize_query(query)
    if not query:
        return None

    lrc = syncedlyrics.search(
        query,
        synced_only=True,
        providers=SYNCED_LYRICS_PROVIDERS,
    )
    if not lrc:
        return None

    lines = parse_lrc(lrc)
    if not lines:
        return None
    return SyncedLyrics(query=query, lines=lines)


def search_synced_lyrics_sync(track: Track) -> SyncedLyrics | None:
    return search_synced_lyrics_for_query_sync(synced_lyrics_query(track))


def escape_lyric_text(value: str, *, limit: int = 240) -> str:
    value = clamp_embed_text(value, limit=limit)
    return discord.utils.escape_markdown(value)


def synced_lyrics_block(track: Track, elapsed: float) -> str | None:
    if track.synced_lyrics_status == "loading":
        return "`Finding synced lyrics...`"
    if not track.synced_lyrics:
        return None

    lines = track.synced_lyrics.lines
    if not lines:
        return None

    timestamps = [line.timestamp for line in lines]
    lyric_elapsed = max(0.0, elapsed + SYNCED_LYRICS_OFFSET_SECONDS)
    current_index = bisect.bisect_right(timestamps, lyric_elapsed) - 1
    if current_index < 0:
        start = 0
        end = min(len(lines), SYNCED_LYRICS_CONTEXT_LINES + 1)
        return clamp_embed_text(
            "\n".join(escape_lyric_text(line.text) for line in lines[start:end]),
            limit=1000,
        )

    start = max(0, current_index - SYNCED_LYRICS_CONTEXT_LINES)
    end = min(len(lines), current_index + SYNCED_LYRICS_CONTEXT_LINES + 1)
    display_lines: list[str] = []

    for index in range(start, end):
        text = escape_lyric_text(lines[index].text)
        if index == current_index:
            display_lines.append(f"**{text}**")
        else:
            display_lines.append(text)

    return clamp_embed_text("\n".join(display_lines), limit=1000)


def clamp_embed_text(value: str, limit: int = 3900) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 4].rstrip() + "\n..."


def make_embed(
    title: str,
    description: str | None = None,
    *,
    color: int = PRIMARY_COLOR,
    requester: discord.abc.User | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )
    if requester:
        embed.set_footer(
            text=f"Requested by {requester.display_name}",
            icon_url=requester.display_avatar.url,
        )
    else:
        embed.set_footer(text=BOT_NAME)
    return embed


def track_line(
    title: str,
    url: str | None = None,
    *,
    artist: str | None = None,
) -> str:
    song, artist = display_song_artist(title, artist)
    song_text = f"[{song}]({url})" if url else song
    if artist:
        return f"**{song_text}** by *{artist}*"
    return f"**{song_text}**"


def queue_item_info(item: QueueItem) -> dict[str, Any]:
    return item.info or {}


def queue_item_author(item: QueueItem) -> str:
    info = queue_item_info(item)
    return (
        item.artist
        or info.get("artist")
        or info.get("uploader")
        or info.get("channel")
        or "Unknown"
    )


def queue_item_display_title(item: QueueItem) -> str:
    song, artist = display_song_artist(item.title, item.artist)
    if artist:
        return f"{artist} - {song}"
    return song


def queue_item_duration(item: QueueItem) -> str:
    info = queue_item_info(item)
    duration = info.get("duration")
    if duration is None and info.get("duration_ms") is not None:
        duration = int(info["duration_ms"]) / 1000
    if duration is None:
        return info.get("duration_string") or "Unknown"

    minutes, seconds = divmod(int(duration), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def queue_item_thumbnail(item: QueueItem) -> str | None:
    info = queue_item_info(item)
    thumbnail = info.get("thumbnail")
    if thumbnail:
        return thumbnail

    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        for candidate in reversed(thumbnails):
            if isinstance(candidate, dict) and candidate.get("url"):
                return candidate["url"]
    return None


def build_track_added_embed(
    item: QueueItem,
    requester: discord.abc.User,
) -> discord.Embed:
    title = queue_item_display_title(item)
    escaped_title = discord.utils.escape_markdown(title)
    embed = make_embed(
        "Track Added",
        f"Added **{escaped_title}** to the queue!",
        color=PRIMARY_COLOR,
        requester=requester,
    )
    embed.add_field(
        name="Author",
        value=discord.utils.escape_markdown(queue_item_author(item)),
        inline=True,
    )
    embed.add_field(
        name="Duration",
        value=queue_item_duration(item),
        inline=True,
    )
    thumbnail = queue_item_thumbnail(item)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


def set_track_requester_footer(embed: discord.Embed, track: Track) -> None:
    if track.requester_avatar_url:
        embed.set_footer(
            text=f"Requested by {track.requester}",
            icon_url=track.requester_avatar_url,
        )
    else:
        embed.set_footer(text=f"Requested by {track.requester}")


def cancel_progress_task(player: GuildPlayer) -> None:
    if player.progress_task and not player.progress_task.done():
        player.progress_task.cancel()
    player.progress_task = None


def cancel_synced_lyrics_task(player: GuildPlayer) -> None:
    if player.synced_lyrics_task and not player.synced_lyrics_task.done():
        player.synced_lyrics_task.cancel()
    player.synced_lyrics_task = None


def cancel_idle_disconnect_task(player: GuildPlayer) -> None:
    if player.idle_disconnect_task and not player.idle_disconnect_task.done():
        player.idle_disconnect_task.cancel()
    player.idle_disconnect_task = None


def remember_now_playing_message(player: GuildPlayer, message: discord.Message) -> None:
    player.now_playing_message = message
    player.now_playing_messages = [
        known_message
        for known_message in player.now_playing_messages
        if known_message.id != message.id
    ]
    player.now_playing_messages.append(message)


def reset_progress_state(player: GuildPlayer) -> None:
    cancel_progress_task(player)
    cancel_synced_lyrics_task(player)
    cancel_idle_disconnect_task(player)
    player.now_playing_message = None
    player.now_playing_messages.clear()
    player.synced_lyrics_message = None
    player.playback_started_at = None
    player.paused_started_at = None
    player.total_paused_seconds = 0.0


def detach_now_playing_messages(
    player: GuildPlayer,
    *,
    reset_timing: bool = True,
) -> list[discord.Message]:
    messages = list(player.now_playing_messages)
    if player.now_playing_message and all(
        message.id != player.now_playing_message.id for message in messages
    ):
        messages.append(player.now_playing_message)
    cancel_progress_task(player)
    if reset_timing:
        cancel_synced_lyrics_task(player)
        if player.synced_lyrics_message and all(
            message.id != player.synced_lyrics_message.id for message in messages
        ):
            messages.append(player.synced_lyrics_message)
        player.synced_lyrics_message = None
    player.now_playing_message = None
    player.now_playing_messages.clear()
    if reset_timing:
        player.playback_started_at = None
        player.paused_started_at = None
        player.total_paused_seconds = 0.0
    return messages


async def delete_messages(messages: list[discord.Message]) -> None:
    seen_ids: set[int] = set()
    for message in messages:
        if message.id in seen_ids:
            continue
        seen_ids.add(message.id)
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        except discord.HTTPException as exc:
            logger.warning("Could not delete old now-playing embed: %s", exc)


async def clear_now_playing_embeds(
    player: GuildPlayer,
    *,
    reset_timing: bool = True,
) -> None:
    await delete_messages(
        detach_now_playing_messages(player, reset_timing=reset_timing)
    )


def current_elapsed_time(player: GuildPlayer) -> float:
    if player.playback_started_at is None:
        return 0.0

    now = player.paused_started_at or time.monotonic()
    elapsed = now - player.playback_started_at - player.total_paused_seconds
    if player.current and player.current.duration:
        elapsed = min(elapsed, player.current.duration)
    return max(0.0, elapsed)


def current_elapsed_seconds(player: GuildPlayer) -> int:
    return int(current_elapsed_time(player))


def progress_bar(elapsed: int, total: int | None, width: int = 24) -> str:
    if not total:
        return f"`{duration_text(elapsed)} | live/unknown`"

    elapsed = max(0, min(elapsed, total))
    marker = round((elapsed / total) * (width - 1)) if total else 0
    bar = "".join("●" if index == marker else "━" for index in range(width))
    return f"`{duration_text(elapsed)} | {bar} | {duration_text(total)}`"


def build_now_playing_embed(player: GuildPlayer, track: Track) -> discord.Embed:
    elapsed = current_elapsed_time(player)
    embed = make_embed(
        "Now Playing:",
        f"{track_line(track.title, track.webpage_url, artist=track.artist)}\n\n"
        f"{progress_bar(int(elapsed), track.duration)}",
        color=PRIMARY_COLOR,
    )
    set_track_requester_footer(embed, track)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def build_synced_lyrics_embed(player: GuildPlayer, track: Track) -> discord.Embed:
    elapsed = current_elapsed_time(player)
    lyrics = synced_lyrics_block(track, elapsed)
    if not lyrics:
        lyrics = "`No synced lyric line is available yet.`"

    embed = make_embed(
        "Synced Lyrics",
        f"{track_line(track.title, track.webpage_url, artist=track.artist)}\n\n"
        f"{progress_bar(int(elapsed), track.duration)}",
        color=PRIMARY_COLOR,
    )
    embed.add_field(name="Lyrics", value=lyrics, inline=False)
    set_track_requester_footer(embed, track)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def format_synced_lyrics_text(lyrics: SyncedLyrics, *, limit: int = 3500) -> str:
    lines = [
        f"`{duration_text(int(line.timestamp))}` {escape_lyric_text(line.text)}"
        for line in lyrics.lines
    ]
    return clamp_embed_text("\n".join(lines), limit=limit)


async def refresh_now_playing_message(player: GuildPlayer) -> None:
    if not player.now_playing_message or not player.current:
        return
    try:
        await player.now_playing_message.edit(
            embed=build_now_playing_embed(player, player.current)
        )
    except (discord.NotFound, discord.Forbidden):
        failed_message_id = player.now_playing_message.id
        player.now_playing_message = None
        player.now_playing_messages = [
            message
            for message in player.now_playing_messages
            if message.id != failed_message_id
        ]
        cancel_progress_task(player)
    except discord.HTTPException as exc:
        logger.warning("Could not edit now-playing embed: %s", exc)


async def progress_updater(guild_id: int) -> None:
    try:
        while True:
            await asyncio.sleep(NOW_PLAYING_UPDATE_SECONDS)
            player = player_for(guild_id)
            if not player.current or not player.now_playing_message:
                return
            if not player.voice_client or not player.voice_client.is_connected():
                return
            await refresh_now_playing_message(player)
    except asyncio.CancelledError:
        return


def start_progress_task(guild_id: int) -> None:
    player = player_for(guild_id)
    cancel_progress_task(player)
    player.progress_task = asyncio.create_task(progress_updater(guild_id))


def synced_lyrics_available() -> bool:
    return SYNCED_LYRICS_ENABLED and syncedlyrics is not None


async def refresh_synced_lyrics_message(player: GuildPlayer) -> None:
    if not player.synced_lyrics_message or not player.current:
        return
    try:
        await player.synced_lyrics_message.edit(
            embed=build_synced_lyrics_embed(player, player.current)
        )
    except (discord.NotFound, discord.Forbidden):
        player.synced_lyrics_message = None
        cancel_synced_lyrics_task(player)
    except discord.HTTPException as exc:
        logger.warning("Could not edit synced-lyrics embed: %s", exc)


async def synced_lyrics_updater(guild_id: int, track: Track) -> None:
    try:
        while True:
            await asyncio.sleep(NOW_PLAYING_UPDATE_SECONDS)
            player = player_for(guild_id)
            if player.current is not track or not player.synced_lyrics_message:
                return
            if not player.voice_client or not player.voice_client.is_connected():
                return
            await refresh_synced_lyrics_message(player)
    except asyncio.CancelledError:
        return


def start_synced_lyrics_display_task(guild_id: int, track: Track) -> None:
    player = player_for(guild_id)
    cancel_synced_lyrics_task(player)
    player.synced_lyrics_task = asyncio.create_task(
        synced_lyrics_updater(guild_id, track)
    )


async def clear_synced_lyrics_embed(player: GuildPlayer) -> None:
    cancel_synced_lyrics_task(player)
    message = player.synced_lyrics_message
    player.synced_lyrics_message = None
    if not message:
        return
    await delete_messages([message])


async def reply_embed(
    ctx: commands.Context,
    title: str,
    description: str | None = None,
    *,
    color: int = PRIMARY_COLOR,
    requester: discord.abc.User | None = None,
) -> discord.Message:
    return await ctx.reply(
        embed=make_embed(title, description, color=color, requester=requester),
        mention_author=False,
    )


async def send_embed(
    channel: discord.abc.Messageable | None,
    title: str,
    description: str | None = None,
    *,
    color: int = PRIMARY_COLOR,
) -> None:
    if not channel:
        return
    try:
        await channel.send(embed=make_embed(title, description, color=color))
    except discord.Forbidden:
        logger.warning("Missing permission to send a message.")


def player_is_idle(player: GuildPlayer) -> bool:
    voice_client = player.voice_client
    return bool(
        voice_client
        and voice_client.is_connected()
        and not voice_client.is_playing()
        and not voice_client.is_paused()
        and player.current is None
        and not player.queue
        and not player.playback_active
    )


async def idle_disconnect_after(guild_id: int) -> None:
    try:
        await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
        player = player_for(guild_id)
        voice_client = player.voice_client
        if not player_is_idle(player) or not voice_client:
            return

        old_now_playing_messages: list[discord.Message] = []
        async with player.lock:
            voice_client = player.voice_client
            if not player_is_idle(player) or not voice_client:
                return
            player.voice_client = None
            player.loop_current = False
            old_now_playing_messages = detach_now_playing_messages(player)

        await delete_messages(old_now_playing_messages)
        try:
            if voice_client.is_connected():
                await voice_client.disconnect(force=True)
        except discord.HTTPException as exc:
            logger.warning("Could not auto-disconnect idle voice client: %s", exc)

        await send_embed(
            player.text_channel,
            "Disconnected",
            f"Left voice after {duration_text(IDLE_DISCONNECT_SECONDS)} of inactivity.",
            color=SUCCESS_COLOR,
        )
    except asyncio.CancelledError:
        return
    finally:
        player = player_for(guild_id)
        if player.idle_disconnect_task is asyncio.current_task():
            player.idle_disconnect_task = None


def start_idle_disconnect_task(guild_id: int) -> None:
    player = player_for(guild_id)
    cancel_idle_disconnect_task(player)
    if IDLE_DISCONNECT_SECONDS <= 0 or not player_is_idle(player):
        return
    player.idle_disconnect_task = asyncio.create_task(idle_disconnect_after(guild_id))


class QueueView(discord.ui.View):
    def __init__(self, player: GuildPlayer, requester: discord.abc.User) -> None:
        super().__init__(timeout=120)
        self.player = player
        self.requester = requester
        self.page = 0
        self.message: discord.Message | None = None
        self.update_buttons()

    @property
    def total_pages(self) -> int:
        if not self.player.queue:
            return 1
        return max(1, (len(self.player.queue) + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)

    def update_buttons(self) -> None:
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            if item.label == "Previous":
                item.disabled = self.page <= 0
            elif item.label == "Next":
                item.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        self.page = min(self.page, self.total_pages - 1)
        embed = make_embed("Music Queue", color=PRIMARY_COLOR)
        current = self.player.current

        if current:
            now_playing = track_line(
                current.title,
                current.webpage_url,
                artist=current.artist,
            )
            if current.thumbnail:
                embed.set_thumbnail(url=current.thumbnail)
        else:
            now_playing = "*Nothing playing right now.*"

        queue_items = list(self.player.queue)
        start = self.page * QUEUE_PAGE_SIZE
        page_items = queue_items[start : start + QUEUE_PAGE_SIZE]

        if page_items:
            lines = [
                f"{start + index}. "
                f"{track_line(item.title, item.webpage_url, artist=item.artist)}"
                for index, item in enumerate(page_items, start=1)
            ]
            remaining = len(queue_items) - (start + len(page_items))
            if remaining > 0:
                lines.append(f"...and **{remaining}** more track(s)")
            up_next = "\n".join(lines)
        else:
            up_next = "*The queue is empty.*"

        embed.description = (
            f"**Now Playing:**\n{now_playing}\n\n"
            f"**Up Next (Page {self.page + 1}/{self.total_pages}):**\n{up_next}"
        )
        embed.set_footer(
            text=f"Requested by {self.requester.display_name}",
            icon_url=self.requester.display_avatar.url,
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester.id:
            return True
        await interaction.response.send_message(
            embed=make_embed(
                "Queue controls",
                "Only the person who opened this queue can use these buttons.",
                color=WARNING_COLOR,
            ),
            ephemeral=True,
        )
        return False

    async def refresh(self, interaction: discord.Interaction) -> None:
        embed = self.build_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        await self.refresh(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def ytdl_extract_sync(query: str, *, flat: bool = False) -> dict[str, Any]:
    opts = dict(YTDL_OPTIONS)
    if flat:
        opts["extract_flat"] = "in_playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


async def ytdl_extract(query: str, *, flat: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(ytdl_extract_sync, query, flat=flat)


def spotify_track_to_item(
    track: dict[str, Any],
    requester: str,
    requester_avatar_url: str | None,
) -> QueueItem | None:
    title = track.get("name")
    artists = ", ".join(artist["name"] for artist in track.get("artists", []))
    if not title or not artists:
        return None

    images = track.get("album", {}).get("images", [])
    thumbnail = images[0]["url"] if images else None
    return metadata_to_queue_item(
        title=title,
        requester=requester,
        requester_avatar_url=requester_avatar_url,
        artist=artists,
        webpage_url=track.get("external_urls", {}).get("spotify"),
        duration=(
            int(track["duration_ms"]) / 1000
            if track.get("duration_ms") is not None
            else None
        ),
        thumbnail=thumbnail,
    )


def spotify_items_sync(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    if spotify_client is None:
        raise RuntimeError(
            "Spotify support needs SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
        )

    clean_url = url.split("?")[0]
    items: list[QueueItem] = []

    if "/track/" in clean_url:
        track = spotify_client.track(clean_url)
        item = spotify_track_to_item(track, requester, requester_avatar_url)
        return [item] if item else []

    if "/album/" in clean_url:
        page = spotify_client.album_tracks(clean_url, limit=50)
        while page and len(items) < MAX_SPOTIFY_TRACKS:
            for track in page.get("items", []):
                item = spotify_track_to_item(track, requester, requester_avatar_url)
                if item:
                    items.append(item)
                if len(items) >= MAX_SPOTIFY_TRACKS:
                    break
            page = spotify_client.next(page) if page.get("next") else None
        return items

    if "/playlist/" in clean_url:
        page = spotify_client.playlist_items(
            clean_url,
            fields="items.track(name,artists(name),duration_ms,album(images(url)),external_urls),next",
            limit=100,
        )
        while page and len(items) < MAX_SPOTIFY_TRACKS:
            for entry in page.get("items", []):
                track = entry.get("track")
                if not track:
                    continue
                item = spotify_track_to_item(track, requester, requester_avatar_url)
                if item:
                    items.append(item)
                if len(items) >= MAX_SPOTIFY_TRACKS:
                    break
            page = spotify_client.next(page) if page.get("next") else None
        return items

    if "/artist/" in clean_url:
        top_tracks = spotify_client.artist_top_tracks(clean_url)
        for track in top_tracks.get("tracks", []):
            item = spotify_track_to_item(track, requester, requester_avatar_url)
            if item:
                items.append(item)
        return items

    raise RuntimeError("That Spotify URL type is not supported yet.")


async def spotify_items(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    return await asyncio.to_thread(
        spotify_items_sync,
        url,
        requester,
        requester_avatar_url,
    )


def deezer_track_to_item(
    track: dict[str, Any],
    requester: str,
    requester_avatar_url: str | None,
    *,
    fallback_thumbnail: str | None = None,
) -> QueueItem | None:
    artist = track.get("artist")
    album = track.get("album")
    return metadata_to_queue_item(
        title=track.get("title") or track.get("title_short") or "",
        artist=artist.get("name") if isinstance(artist, dict) else None,
        requester=requester,
        requester_avatar_url=requester_avatar_url,
        webpage_url=track.get("link"),
        duration=track.get("duration"),
        thumbnail=(
            (album.get("cover_medium") if isinstance(album, dict) else None)
            or fallback_thumbnail
        ),
    )


def deezer_items_sync(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=15,
        allow_redirects=True,
    )
    response.raise_for_status()
    clean_url = response.url
    match = re.search(r"/(track|album|playlist|artist)/(\d+)", clean_url)
    if not match:
        raise RuntimeError("That Deezer URL type is not supported yet.")

    kind, deezer_id = match.groups()
    if kind == "track":
        data = http_get_json(f"https://api.deezer.com/track/{deezer_id}")
        item = deezer_track_to_item(data, requester, requester_avatar_url)
        return [item] if item else []

    if kind in {"album", "playlist"}:
        data = http_get_json(f"https://api.deezer.com/{kind}/{deezer_id}")
        tracks = data.get("tracks", {}).get("data", [])
        thumbnail = data.get("cover_medium") or data.get("picture_medium")
        items = [
            item
            for track in tracks[:MAX_SPOTIFY_TRACKS]
            if (
                item := deezer_track_to_item(
                    track,
                    requester,
                    requester_avatar_url,
                    fallback_thumbnail=thumbnail,
                )
            )
        ]
        return items

    if kind == "artist":
        data = http_get_json(
            f"https://api.deezer.com/artist/{deezer_id}/top",
            params={"limit": min(MAX_SPOTIFY_TRACKS, 100)},
        )
        tracks = data.get("data", [])
        return [
            item
            for track in tracks
            if (item := deezer_track_to_item(track, requester, requester_avatar_url))
        ]

    raise RuntimeError("That Deezer URL type is not supported yet.")


async def deezer_items(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    return await asyncio.to_thread(
        deezer_items_sync,
        url,
        requester,
        requester_avatar_url,
    )


def apple_artwork_url(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", value)


def apple_song_to_item(
    song: dict[str, Any],
    requester: str,
    requester_avatar_url: str | None,
) -> QueueItem | None:
    return metadata_to_queue_item(
        title=song.get("trackName") or "",
        artist=song.get("artistName"),
        requester=requester,
        requester_avatar_url=requester_avatar_url,
        webpage_url=song.get("trackViewUrl") or song.get("collectionViewUrl"),
        duration=(
            int(song["trackTimeMillis"]) / 1000
            if song.get("trackTimeMillis") is not None
            else None
        ),
        thumbnail=apple_artwork_url(song.get("artworkUrl100")),
    )


def apple_music_lookup_ids(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    track_id = (query.get("i") or [None])[0]
    path_ids = re.findall(r"/(\d+)(?:[/?#]|$)", parsed.path)
    page_id = path_ids[-1] if path_ids else None

    if "/song/" in parsed.path and not track_id:
        track_id = page_id
    album_id = page_id if "/album/" in parsed.path else None
    return track_id, album_id


def apple_music_items_sync(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    track_id, album_id = apple_music_lookup_ids(url)
    lookup_id = track_id or album_id
    if not lookup_id:
        raise RuntimeError("That Apple Music URL type is not supported yet.")

    data = http_get_json(
        "https://itunes.apple.com/lookup",
        params={
            "id": lookup_id,
            "entity": "song",
            "limit": min(MAX_SPOTIFY_TRACKS, 200),
        },
    )
    songs = [
        result
        for result in data.get("results", [])
        if result.get("wrapperType") == "track"
    ]
    if track_id:
        songs = [song for song in songs if str(song.get("trackId")) == str(track_id)]
    return [
        item
        for song in songs[:MAX_SPOTIFY_TRACKS]
        if (item := apple_song_to_item(song, requester, requester_avatar_url))
    ]


async def apple_music_items(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    return await asyncio.to_thread(
        apple_music_items_sync,
        url,
        requester,
        requester_avatar_url,
    )


def scraped_music_item_sync(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
    *,
    provider: str,
) -> list[QueueItem]:
    html = http_get_text(url)
    title: str | None = None
    artist: str | None = None
    thumbnail: str | None = None

    for raw_json_ld in iter_json_ld(html):
        for entry in flatten_json_ld(raw_json_ld):
            entry_type = entry.get("@type")
            if isinstance(entry_type, list):
                entry_types = {str(item).lower() for item in entry_type}
            else:
                entry_types = {str(entry_type).lower()} if entry_type else set()
            if not entry_types & {"musicrecording", "song", "musicalbum"}:
                continue
            title = title or json_ld_text(entry.get("name"))
            artist = artist or json_ld_text(
                entry.get("byArtist") or entry.get("artist")
            )
            image = entry.get("image")
            thumbnail = thumbnail or json_ld_text(image)
            if title:
                break
        if title:
            break

    title = title or first_meta_content(html, "og:title", "twitter:title")
    thumbnail = thumbnail or first_meta_content(html, "og:image", "twitter:image")
    if not title:
        title = first_html_title(html)
    if not title:
        raise RuntimeError(f"Could not read metadata from that {provider} URL.")

    if not artist:
        title, artist = split_title_artist_from_page(title, provider)
    else:
        title = clean_provider_title(title, provider)

    item = metadata_to_queue_item(
        title=title,
        artist=artist,
        requester=requester,
        requester_avatar_url=requester_avatar_url,
        webpage_url=url,
        thumbnail=thumbnail,
    )
    return [item] if item else []


async def scraped_music_items(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
    *,
    provider: str,
) -> list[QueueItem]:
    return await asyncio.to_thread(
        scraped_music_item_sync,
        url,
        requester,
        requester_avatar_url,
        provider=provider,
    )


def iter_embedded_json(html: str) -> list[Any]:
    values: list[Any] = []

    for match in re.finditer(
        r"data-a-state\s*=\s*([\"'])(.*?)\1",
        html,
        flags=re.IGNORECASE | re.S,
    ):
        raw_json = unescape(match.group(2)).strip()
        if not raw_json:
            continue
        try:
            values.append(json.loads(raw_json))
        except json.JSONDecodeError:
            continue

    for match in re.finditer(
        r"<script\b[^>]*>(.*?)</script>",
        html,
        flags=re.IGNORECASE | re.S,
    ):
        script = unescape(match.group(1)).strip()
        if not script:
            continue

        try:
            values.append(json.loads(script))
            continue
        except json.JSONDecodeError:
            pass

        for parse_match in re.finditer(
            r"JSON\.parse\((?P<quoted>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*')\)",
            script,
            flags=re.S,
        ):
            try:
                decoded = json.loads(parse_match.group("quoted"))
                values.append(json.loads(decoded))
            except (TypeError, json.JSONDecodeError):
                continue

    return values


def walk_json(value: Any) -> list[Any]:
    values = [value]
    if isinstance(value, dict):
        for child in value.values():
            values.extend(walk_json(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(walk_json(child))
    return values


def first_nested_text(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in keys:
                text = json_ld_text(child)
                if text:
                    return sanitize_query(text)
    return None


def first_nested_url(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() not in keys:
                continue
            if isinstance(child, str) and child.startswith(("http://", "https://")):
                return child
            if isinstance(child, dict):
                text = first_nested_url(child, keys | {"url"})
                if text:
                    return text
            if isinstance(child, list):
                for entry in child:
                    text = first_nested_url(entry, keys | {"url"})
                    if text:
                        return text
    return None


def first_nested_duration(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    for key, child in value.items():
        lowered = key.lower()
        if lowered in {"duration", "durationseconds", "durationinseconds"}:
            try:
                return int(float(child))
            except (TypeError, ValueError):
                continue
        if lowered in {"durationms", "durationmillis", "durationmilliseconds"}:
            try:
                return int(float(child) / 1000)
            except (TypeError, ValueError):
                continue
    return None


def amazon_track_candidates(html: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    json_values = iter_json_ld(html) + iter_embedded_json(html)

    for root in json_values:
        for value in walk_json(root):
            if not isinstance(value, dict):
                continue

            item_type = str(
                value.get("@type")
                or value.get("type")
                or value.get("entityType")
                or value.get("contentType")
                or ""
            ).lower()
            title = first_nested_text(
                value,
                {
                    "tracktitle",
                    "songtitle",
                    "titlename",
                    "title",
                    "name",
                },
            )
            artist = first_nested_text(
                value,
                {
                    "artistname",
                    "primaryartistname",
                    "artistdisplayname",
                    "artist",
                    "artists",
                    "byartist",
                },
            )
            if not title or not artist:
                continue

            looks_like_track = (
                "track" in item_type
                or "song" in item_type
                or any(
                    key.lower()
                    in {"trackasin", "tracktitle", "songtitle", "durationms"}
                    for key in value.keys()
                )
            )
            if not looks_like_track:
                continue

            key = (normalize_artist_match(title), normalize_artist_match(artist))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "title": title,
                    "artist": artist,
                    "duration": first_nested_duration(value),
                    "thumbnail": first_nested_url(
                        value,
                        {
                            "image",
                            "imageurl",
                            "albumart",
                            "albumarturl",
                            "coverart",
                            "coverarturl",
                            "artwork",
                            "url",
                        },
                    ),
                }
            )

    return candidates


def amazon_page_kind(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/playlists/" in path:
        return "playlist"
    if parse_qs(parsed.query).get("trackAsin"):
        return "track"
    if "/tracks/" in path:
        return "track"
    if "/albums/" in path:
        return "album"
    return "track"


def amazon_web_api_endpoint(host: str) -> str:
    if host.endswith(".co.uk") or host.endswith(".de") or host.endswith(".fr"):
        return "https://eu.web.skill.music.a2z.com/api/showHome"
    return "https://na.web.skill.music.a2z.com/api/showHome"


def amazon_config_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(path="/config.json", params="", fragment="").geturl()


def amazon_deeplink(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    kept_query: list[str] = []
    if query.get("trackAsin"):
        kept_query.append(f"trackAsin={query['trackAsin'][0]}")
    return parsed.path + (f"?{'&'.join(kept_query)}" if kept_query else "")


def amazon_currency_for_host(host: str) -> str:
    if host.endswith(".co.uk"):
        return "GBP"
    if host.endswith(".de") or host.endswith(".fr"):
        return "EUR"
    return "USD"


def amazon_show_home_json(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    config = http_get_json(amazon_config_url(url))
    csrf = config.get("csrf") or {}
    host = parsed.netloc
    user_agent = REQUEST_HEADERS["User-Agent"]
    headers_payload = {
        "x-amzn-authentication": json.dumps(
            {
                "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
                "accessToken": config.get("accessToken", ""),
            }
        ),
        "x-amzn-device-model": "WEBPLAYER",
        "x-amzn-device-width": "1920",
        "x-amzn-device-family": "WebPlayer",
        "x-amzn-device-id": config.get("deviceId", ""),
        "x-amzn-user-agent": user_agent,
        "x-amzn-session-id": config.get("sessionId", ""),
        "x-amzn-device-height": "1080",
        "x-amzn-request-id": str(uuid.uuid4()),
        "x-amzn-device-language": config.get("displayLanguage", "en_GB"),
        "x-amzn-currency-of-preference": amazon_currency_for_host(host),
        "x-amzn-os-version": "1.0",
        "x-amzn-application-version": config.get("version", "1.0.0"),
        "x-amzn-device-time-zone": os.getenv("TZ", "Europe/London"),
        "x-amzn-timestamp": str(int(time.time() * 1000)),
        "x-amzn-csrf": json.dumps(
            {
                "interface": "CSRFInterface.v1_0.CSRFHeaderElement",
                "token": csrf.get("token", ""),
                "timestamp": csrf.get("ts", ""),
                "rndNonce": csrf.get("rnd", ""),
            }
        ),
        "x-amzn-music-domain": host,
        "x-amzn-referer": "",
        "x-amzn-affiliate-tags": "",
        "x-amzn-ref-marker": (config.get("metricsContext") or {}).get("refMarker", ""),
        "x-amzn-page-url": url,
        "x-amzn-weblab-id-overrides": "",
        "x-amzn-video-player-token": "",
        "x-amzn-feature-flags": "",
        "x-amzn-has-profile-id": "",
        "x-amzn-age-band": "",
    }
    body = {
        "deeplink": json.dumps(
            {
                "interface": "DeeplinkInterface.v1_0.DeeplinkClientInformation",
                "deeplink": amazon_deeplink(url),
            }
        ),
        "headers": json.dumps(headers_payload),
    }
    response = requests.post(
        amazon_web_api_endpoint(host),
        headers={
            **REQUEST_HEADERS,
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/json",
            "Origin": f"{parsed.scheme}://{host}",
            "Referer": f"{parsed.scheme}://{host}/",
        },
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def amazon_seo_artist(value: Any) -> str | None:
    titles: list[str] = []
    for item in walk_json(value):
        if not isinstance(item, dict):
            continue
        if item.get("interface") == "Web.PageInterface.v1_0.SEOHeadLDJSONScriptElement":
            raw = item.get("innerHTML")
            if not isinstance(raw, str):
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data_type = str(data.get("@type") or "").lower()
            if data_type not in {"musicalbum", "musicrecording", "song"}:
                continue
            artist = json_ld_text(data.get("byArtist") or data.get("artist"))
            if artist:
                return sanitize_query(artist)

        title = item.get("title")
        if isinstance(title, str):
            titles.append(title)

    for title in titles:
        match = re.match(
            r"^Play\s+.+?\s+by\s+(.+?)\s+on\s+Amazon Music",
            title,
            flags=re.IGNORECASE,
        )
        if match:
            return sanitize_query(match.group(1))
    return None


def amazon_response_tracks(value: Any, page_kind: str) -> list[dict[str, Any]]:
    fallback_artist = amazon_seo_artist(value)
    tracks: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    row_interfaces = {
        "Web.TemplatesInterface.v1_0.Touch.WidgetsInterface.VisualRowItemElement",
        "Web.TemplatesInterface.v1_0.Touch.WidgetsInterface.DescriptiveRowItemElement",
    }

    for item in walk_json(value):
        if not isinstance(item, dict) or item.get("interface") not in row_interfaces:
            continue
        title = sanitize_query(item.get("primaryText") or "")
        if not title:
            continue

        deeplink = ""
        primary_link = item.get("primaryLink") or item.get("primaryTextLink")
        if isinstance(primary_link, dict):
            deeplink = primary_link.get("deeplink") or ""
        if not deeplink and page_kind in {"playlist", "album"}:
            continue

        artist = sanitize_query(item.get("secondaryText1") or fallback_artist or "")
        if not artist:
            continue

        duration = parse_duration_value(
            item.get("secondaryText3") or item.get("duration")
        )
        key = (normalize_artist_match(title), normalize_artist_match(artist), deeplink)
        if key in seen:
            continue
        seen.add(key)
        tracks.append(
            {
                "title": title,
                "artist": artist,
                "duration": duration,
                "thumbnail": first_nested_url(
                    item,
                    {
                        "image",
                        "imageurl",
                        "albumart",
                        "albumarturl",
                        "coverart",
                        "coverarturl",
                        "artwork",
                        "url",
                    },
                ),
            }
        )

    return tracks


def amazon_music_items_sync(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    parsed = urlparse(url)
    kind = amazon_page_kind(url)
    candidates: list[dict[str, Any]] = []

    try:
        candidates = amazon_response_tracks(amazon_show_home_json(url), kind)
    except Exception as exc:
        logger.warning("Amazon Music API metadata lookup failed: %s", exc)

    if not candidates:
        response = http_get_response(url)
        html = response.text
        candidates = amazon_track_candidates(html)

    if candidates:
        selected = (
            candidates[:MAX_SPOTIFY_TRACKS]
            if kind in {"album", "playlist"}
            else candidates[:1]
        )
        items = [
            item
            for candidate in selected
            if (
                item := metadata_to_queue_item(
                    title=candidate["title"],
                    artist=candidate.get("artist"),
                    requester=requester,
                    requester_avatar_url=requester_avatar_url,
                    webpage_url=url,
                    duration=candidate.get("duration"),
                    thumbnail=candidate.get("thumbnail"),
                )
            )
        ]
        if items:
            return items

    # Last-resort fallback for Amazon pages that hide their track list from HTML.
    # This still lets single-track links resolve through title/artist page metadata.
    return scraped_music_item_sync(
        url,
        requester,
        requester_avatar_url,
        provider="Amazon Music",
    )


async def amazon_music_items(
    url: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    return await asyncio.to_thread(
        amazon_music_items_sync,
        url,
        requester,
        requester_avatar_url,
    )


def entry_to_queue_item(
    entry: dict[str, Any],
    requester: str,
    requester_avatar_url: str | None,
) -> QueueItem | None:
    title = entry.get("title") or "Queued track"
    artist = entry.get("artist")
    webpage_url = entry.get("webpage_url")
    query = webpage_url or entry.get("url")
    if not query:
        return None

    info = entry if entry.get("url") and entry.get("webpage_url") else None
    return QueueItem(
        query=query,
        title=title,
        requester=requester,
        requester_avatar_url=requester_avatar_url,
        artist=artist,
        webpage_url=webpage_url,
        info=info,
    )


async def build_queue_items(
    query: str,
    requester: str,
    requester_avatar_url: str | None,
) -> list[QueueItem]:
    query = sanitize_query(query)
    if is_spotify_url(query):
        return await spotify_items(query, requester, requester_avatar_url)
    if is_deezer_url(query):
        return await deezer_items(query, requester, requester_avatar_url)
    if is_apple_music_url(query):
        return await apple_music_items(query, requester, requester_avatar_url)
    if is_tidal_url(query):
        return await scraped_music_items(
            query,
            requester,
            requester_avatar_url,
            provider="TIDAL",
        )
    if is_amazon_music_url(query):
        return await amazon_music_items(query, requester, requester_avatar_url)

    lookup = query if is_url(query) else f"ytsearch1:{query}"
    info = await ytdl_extract(lookup, flat=should_flat_extract_url(query))

    entries = info.get("entries")
    if entries:
        items = [
            item
            for entry in entries
            if entry
            and (item := entry_to_queue_item(entry, requester, requester_avatar_url))
        ]
        return items

    item = entry_to_queue_item(info, requester, requester_avatar_url)
    return [item] if item else []


async def resolve_queue_item(item: QueueItem) -> Track:
    info = item.info
    source_info = info or {}
    if not info or not info.get("url"):
        info = await ytdl_extract(item.query)

    if "entries" in info and info["entries"]:
        info = next(entry for entry in info["entries"] if entry)

    stream_url = info.get("url")
    if not stream_url:
        raise RuntimeError("yt-dlp did not return a playable audio stream.")

    artist = item.artist or info.get("artist")
    return Track(
        title=info.get("title") or item.title,
        stream_url=stream_url,
        webpage_url=info.get("webpage_url") or item.webpage_url,
        duration=info.get("duration") or source_info.get("duration"),
        requester=item.requester,
        requester_avatar_url=item.requester_avatar_url,
        artist=artist,
        thumbnail=info.get("thumbnail") or source_info.get("thumbnail"),
        uploader=info.get("uploader") or info.get("channel"),
        source_item=QueueItem(
            query=item.query,
            title=item.title,
            requester=item.requester,
            requester_avatar_url=item.requester_avatar_url,
            artist=item.artist,
            webpage_url=item.webpage_url,
            info=None,
        ),
    )


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient | None:
    if not ctx.guild:
        await reply_embed(
            ctx,
            "Server only",
            "This bot only works inside a server.",
            color=ERROR_COLOR,
        )
        return None

    member = ctx.author
    if not isinstance(member, discord.Member) or not member.voice:
        await reply_embed(
            ctx,
            "Join a voice channel",
            "Join a voice channel first, then try again.",
            color=ERROR_COLOR,
        )
        return None

    player = player_for(ctx.guild.id)
    target_channel = member.voice.channel
    voice_client = ctx.guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel != target_channel:
            await voice_client.move_to(target_channel)
    else:
        voice_client = await target_channel.connect()

    player.voice_client = voice_client
    player.text_channel = ctx.channel
    start_idle_disconnect_task(ctx.guild.id)
    return voice_client


def make_audio_source(player: GuildPlayer, track: Track) -> discord.PCMVolumeTransformer:
    source = discord.FFmpegPCMAudio(
        track.stream_url,
        before_options=FFMPEG_BEFORE_OPTIONS,
        options=FFMPEG_OPTIONS,
    )
    return discord.PCMVolumeTransformer(source, volume=player.volume)


def on_track_done(guild_id: int, error: Exception | None) -> None:
    if error:
        logger.error("Playback error in guild %s: %s", guild_id, error)

    async def advance() -> None:
        player = player_for(guild_id)
        async with player.lock:
            player.playback_active = False
            old_now_playing_messages = detach_now_playing_messages(player)
            was_manual_stop = player.manual_stop
            if was_manual_stop:
                player.manual_stop = False
                player.current = None
        await delete_messages(old_now_playing_messages)
        if was_manual_stop:
            return
        await play_next(guild_id)

    bot.loop.call_soon_threadsafe(lambda: asyncio.create_task(advance()))


async def play_next(guild_id: int) -> None:
    player = player_for(guild_id)
    old_now_playing_messages: list[discord.Message] = []
    queue_finished_channel: discord.abc.Messageable | None = None
    item: QueueItem | None = None

    async with player.lock:
        if player.playback_active:
            return
        player.playback_active = True

        if player.loop_current and player.current and not player.skip_requested:
            player.queue.appendleft(player.current.source_item)

        player.skip_requested = False

        if not player.voice_client or not player.voice_client.is_connected():
            player.playback_active = False
            player.current = None
            cancel_idle_disconnect_task(player)
            old_now_playing_messages = detach_now_playing_messages(player)

        elif not player.queue:
            player.playback_active = False
            player.current = None
            old_now_playing_messages = detach_now_playing_messages(player)
            queue_finished_channel = player.text_channel

        else:
            item = player.queue.popleft()

    if old_now_playing_messages:
        await delete_messages(old_now_playing_messages)

    if queue_finished_channel:
        await send_embed(
            queue_finished_channel,
            "Queue finished",
            "There are no more tracks waiting.",
            color=SUCCESS_COLOR,
        )
        start_idle_disconnect_task(guild_id)
        return

    if item is None:
        return

    cancel_idle_disconnect_task(player)

    try:
        track = await resolve_queue_item(item)
    except Exception as exc:
        logger.exception("Could not resolve queued item: %s", item.query)
        await send_embed(
            player.text_channel,
            "Could not play track",
            f"`{item.title}`\n{exc}",
            color=ERROR_COLOR,
        )
        async with player.lock:
            player.playback_active = False
        await play_next(guild_id)
        return

    old_now_playing_messages = []
    should_return = False
    async with player.lock:
        if player.manual_stop:
            player.playback_active = False
            player.current = None
            old_now_playing_messages = detach_now_playing_messages(player)
            should_return = True

        elif not player.voice_client or not player.voice_client.is_connected():
            player.playback_active = False
            player.current = None
            old_now_playing_messages = detach_now_playing_messages(player)
            should_return = True

        else:
            player.current = track
            cancel_idle_disconnect_task(player)
            player.playback_started_at = time.monotonic()
            player.paused_started_at = None
            player.total_paused_seconds = 0.0
            player.voice_client.play(
                make_audio_source(player, track),
                after=lambda error: on_track_done(guild_id, error),
            )

    if old_now_playing_messages:
        await delete_messages(old_now_playing_messages)

    if should_return:
        return

    if player.text_channel:
        message = await player.text_channel.send(
            embed=build_now_playing_embed(player, track)
        )
        remember_now_playing_message(player, message)
        start_progress_task(guild_id)


@bot.event
async def on_ready() -> None:
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name=f"{COMMAND_PREFIX}play | wxterfall",
        )
    )
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    logger.info("Text command prefix: %s", COMMAND_PREFIX)


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    prefix = COMMAND_PREFIX
    embed = make_embed(
        "Simple Music Commands",
        (
            f"`{prefix}play <query or link>` - queue a song, playlist, or supported music-service link\n"
            f"`{prefix}pause` / `{prefix}resume` - control playback\n"
            f"`{prefix}skip` - skip the current song\n"
            f"`{prefix}stop` - clear the queue and leave voice\n"
            f"`{prefix}disconnect` - leave voice\n"
            f"`{prefix}queue` - show the queue\n"
            f"`{prefix}shuffle` - shuffle the remaining queue\n"
            f"`{prefix}now` - show the current song\n"
            f"`{prefix}loop` - toggle repeat for the current song\n"
            f"`{prefix}volume <0-200>` - set playback volume\n"
            f"`{prefix}lyrics [song]` - fetch lyrics with Genius\n"
            f"`{prefix}syncedlyrics [song]` - fetch synced lyrics"
        ),
    )
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="join")
async def join(ctx: commands.Context) -> None:
    voice_client = await ensure_voice(ctx)
    if voice_client:
        await reply_embed(
            ctx,
            "Joined voice",
            f"Connected to **{voice_client.channel}**.",
            color=SUCCESS_COLOR,
        )


@bot.command(name="play", aliases=["p"])
async def play(ctx: commands.Context, *, query: str | None = None) -> None:
    if not ctx.guild:
        return
    if not query:
        await reply_embed(
            ctx,
            "Missing song",
            f"Usage: `{COMMAND_PREFIX}play <query or link>`",
            color=WARNING_COLOR,
            requester=ctx.author,
        )
        return

    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return

    player = player_for(ctx.guild.id)
    cancel_idle_disconnect_task(player)

    async with ctx.typing():
        try:
            items = await build_queue_items(
                query,
                requester=ctx.author.display_name,
                requester_avatar_url=ctx.author.display_avatar.url,
            )
        except Exception as exc:
            logger.exception("Could not build queue items for %s", query)
            await reply_embed(
                ctx,
                "Could not queue that",
                str(exc),
                color=ERROR_COLOR,
                requester=ctx.author,
            )
            return

    if not items:
        await reply_embed(
            ctx,
            "No playable results",
            "I could not find anything playable.",
            color=ERROR_COLOR,
            requester=ctx.author,
        )
        return

    player.queue.extend(items)

    if len(items) == 1:
        await ctx.reply(
            embed=build_track_added_embed(items[0], ctx.author),
            mention_author=False,
        )
    else:
        await reply_embed(
            ctx,
            f"Queued {len(items)} tracks",
            "First up: "
            f"{track_line(items[0].title, items[0].webpage_url, artist=items[0].artist)}",
            color=SUCCESS_COLOR,
            requester=ctx.author,
        )

    await play_next(ctx.guild.id)


@bot.command(name="pause")
async def pause(ctx: commands.Context) -> None:
    voice_client = ctx.guild.voice_client if ctx.guild else None
    if voice_client and voice_client.is_playing():
        player = player_for(ctx.guild.id)
        if player.paused_started_at is None:
            player.paused_started_at = time.monotonic()
        voice_client.pause()
        await refresh_now_playing_message(player)
        await reply_embed(
            ctx,
            "Paused",
            "Playback is paused.",
            color=SUCCESS_COLOR,
            requester=ctx.author,
        )
    else:
        await reply_embed(
            ctx,
            "Nothing playing",
            "There is nothing to pause.",
            color=WARNING_COLOR,
            requester=ctx.author,
        )


@bot.command(name="resume")
async def resume(ctx: commands.Context) -> None:
    voice_client = ctx.guild.voice_client if ctx.guild else None
    if voice_client and voice_client.is_paused():
        player = player_for(ctx.guild.id)
        if player.paused_started_at is not None:
            player.total_paused_seconds += time.monotonic() - player.paused_started_at
        player.paused_started_at = None
        voice_client.resume()
        await refresh_now_playing_message(player)
        await reply_embed(
            ctx,
            "Resumed",
            "Playback is running again.",
            color=SUCCESS_COLOR,
            requester=ctx.author,
        )
    else:
        await reply_embed(
            ctx,
            "Nothing paused",
            "There is no paused track to resume.",
            color=WARNING_COLOR,
            requester=ctx.author,
        )


@bot.command(name="skip")
async def skip(ctx: commands.Context) -> None:
    if not ctx.guild:
        return

    player = player_for(ctx.guild.id)
    voice_client = ctx.guild.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        player.skip_requested = True
        voice_client.stop()
        await reply_embed(
            ctx,
            "Skipped",
            "Moving to the next track.",
            color=SUCCESS_COLOR,
            requester=ctx.author,
        )
    else:
        await reply_embed(
            ctx,
            "Nothing playing",
            "There is nothing to skip.",
            color=WARNING_COLOR,
            requester=ctx.author,
        )


async def clear_music_state_and_disconnect(
    ctx: commands.Context,
    *,
    success_title: str,
    success_description: str,
    empty_title: str,
    empty_description: str,
) -> None:
    if not ctx.guild:
        return

    player = player_for(ctx.guild.id)
    voice_client = ctx.guild.voice_client or player.voice_client
    voice_connected = bool(voice_client and voice_client.is_connected())
    voice_active = bool(
        voice_client
        and voice_connected
        and (voice_client.is_playing() or voice_client.is_paused())
    )
    has_music_state = (
        voice_connected
        or voice_active
        or player.current is not None
        or bool(player.queue)
        or player.playback_active
    )

    if not has_music_state:
        await reply_embed(
            ctx,
            empty_title,
            empty_description,
            color=WARNING_COLOR,
            requester=ctx.author,
        )
        return

    cancel_idle_disconnect_task(player)
    player.queue.clear()
    player.manual_stop = voice_active
    player.loop_current = False
    player.skip_requested = False

    if voice_client:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        if voice_client.is_connected():
            await voice_client.disconnect(force=True)

    old_now_playing_messages: list[discord.Message] = []
    async with player.lock:
        player.playback_active = False
        player.current = None
        player.voice_client = None
        if not voice_active:
            player.manual_stop = False
        old_now_playing_messages = detach_now_playing_messages(player)

    await delete_messages(old_now_playing_messages)

    await reply_embed(
        ctx,
        success_title,
        success_description,
        color=SUCCESS_COLOR,
        requester=ctx.author,
    )


@bot.command(name="stop")
async def stop(ctx: commands.Context) -> None:
    await clear_music_state_and_disconnect(
        ctx,
        success_title="Stopped",
        success_description="Stopped the music and cleared the queue!",
        empty_title="Already stopped",
        empty_description=f"The music is already stopped. Use `{COMMAND_PREFIX}play <song>` to start something.",
    )


@bot.command(name="disconnect", aliases=["dc", "leave"])
async def disconnect(ctx: commands.Context) -> None:
    await clear_music_state_and_disconnect(
        ctx,
        success_title="Disconnected",
        success_description="Disconnected from voice and cleared the queue.",
        empty_title="Already disconnected",
        empty_description="I'm not connected to a voice channel.",
    )


@bot.command(name="queue", aliases=["q"])
async def queue_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return

    player = player_for(ctx.guild.id)
    view = QueueView(player, ctx.author)
    view.message = await ctx.reply(
        embed=view.build_embed(),
        view=view,
        mention_author=False,
    )


@bot.command(name="shuffle", aliases=["mix"])
async def shuffle_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return

    player = player_for(ctx.guild.id)
    if len(player.queue) < 2:
        await reply_embed(
            ctx,
            "Not enough queued",
            "Add at least two upcoming tracks before shuffling.",
            color=WARNING_COLOR,
            requester=ctx.author,
        )
        return

    queued_tracks = list(player.queue)
    random.shuffle(queued_tracks)
    player.queue = deque(queued_tracks)

    await reply_embed(
        ctx,
        "Queue shuffled",
        f"Shuffled **{len(queued_tracks)}** upcoming tracks.",
        color=SUCCESS_COLOR,
        requester=ctx.author,
    )


@bot.command(name="now", aliases=["np", "nowplaying"])
async def now_playing(ctx: commands.Context) -> None:
    if not ctx.guild:
        return

    player = player_for(ctx.guild.id)
    track = player.current
    if not track:
        await reply_embed(
            ctx,
            "Nothing playing",
            "There is no current track.",
            color=WARNING_COLOR,
        )
        return

    await clear_now_playing_embeds(player, reset_timing=False)
    message = await ctx.reply(
        embed=build_now_playing_embed(player, track),
        mention_author=False,
    )
    remember_now_playing_message(player, message)
    start_progress_task(ctx.guild.id)


@bot.command(name="loop")
async def loop(ctx: commands.Context) -> None:
    if not ctx.guild:
        return

    player = player_for(ctx.guild.id)
    player.loop_current = not player.loop_current
    state = "on" if player.loop_current else "off"
    await reply_embed(
        ctx,
        "Loop updated",
        f"Loop is now **{state}**.",
        color=SUCCESS_COLOR,
        requester=ctx.author,
    )


@bot.command(name="volume", aliases=["vol"])
async def volume(ctx: commands.Context, value: int | None = None) -> None:
    if not ctx.guild:
        return
    if value is None:
        await reply_embed(
            ctx,
            "Missing volume",
            f"Usage: `{COMMAND_PREFIX}volume <0-200>`",
            color=WARNING_COLOR,
            requester=ctx.author,
        )
        return
    if value < 0 or value > 200:
        await reply_embed(
            ctx,
            "Volume out of range",
            "Volume must be between 0 and 200.",
            color=WARNING_COLOR,
            requester=ctx.author,
        )
        return

    player = player_for(ctx.guild.id)
    player.volume = value / 100
    voice_client = ctx.guild.voice_client
    if voice_client and isinstance(voice_client.source, discord.PCMVolumeTransformer):
        voice_client.source.volume = player.volume

    await reply_embed(
        ctx,
        "Volume updated",
        f"Volume set to **{value}%**.",
        color=SUCCESS_COLOR,
        requester=ctx.author,
    )


@bot.command(name="syncedlyrics", aliases=["synclyrics", "slrc"])
async def synced_lyrics_command(
    ctx: commands.Context,
    *,
    query: str | None = None,
) -> None:
    if not synced_lyrics_available():
        detail = (
            "`syncedlyrics` is not installed."
            if syncedlyrics is None
            else "Synced lyrics are disabled by `SYNCED_LYRICS_ENABLED`."
        )
        await reply_embed(
            ctx,
            "Synced lyrics unavailable",
            detail,
            color=WARNING_COLOR,
        )
        return

    if query:
        async with ctx.typing():
            lyrics = await asyncio.to_thread(
                search_synced_lyrics_for_query_sync,
                query,
            )

        if not lyrics:
            await reply_embed(
                ctx,
                "Synced lyrics not found",
                "I could not find timestamped lyrics for that.",
                color=ERROR_COLOR,
            )
            return

        embed = make_embed(
            f"Synced Lyrics: {lyrics.query}",
            format_synced_lyrics_text(lyrics),
            requester=ctx.author,
        )
        await ctx.reply(embed=embed, mention_author=False)
        return

    if not ctx.guild:
        return

    player = player_for(ctx.guild.id)
    track = player.current
    if not track:
        await reply_embed(
            ctx,
            "Missing song",
            "Give me a song name or play something first.",
            color=WARNING_COLOR,
        )
        return

    if not track.synced_lyrics:
        track.synced_lyrics_status = "loading"
        async with ctx.typing():
            lyrics = await asyncio.to_thread(search_synced_lyrics_sync, track)
        if player.current is not track:
            track.synced_lyrics_status = None
            await reply_embed(
                ctx,
                "Track changed",
                "The song changed while I was looking up synced lyrics.",
                color=WARNING_COLOR,
            )
            return
        track.synced_lyrics = lyrics
        track.synced_lyrics_status = "ready" if lyrics else None

    if not track.synced_lyrics:
        await reply_embed(
            ctx,
            "Synced lyrics not found",
            "I could not find timestamped lyrics for the current track.",
            color=ERROR_COLOR,
        )
        return

    await clear_synced_lyrics_embed(player)
    message = await ctx.reply(
        embed=build_synced_lyrics_embed(player, track),
        mention_author=False,
    )
    player.synced_lyrics_message = message
    start_synced_lyrics_display_task(ctx.guild.id, track)


@bot.command(name="lyrics")
async def lyrics(ctx: commands.Context, *, query: str | None = None) -> None:
    if genius_client is None:
        await reply_embed(
            ctx,
            "Lyrics unavailable",
            "Lyrics need a `GENIUS_TOKEN` in `.env`.",
            color=WARNING_COLOR,
        )
        return

    if not query:
        if not ctx.guild:
            return
        current = player_for(ctx.guild.id).current
        if not current:
            await reply_embed(
                ctx,
                "Missing song",
                "Give me a song name or play something first.",
                color=WARNING_COLOR,
            )
            return
        song_title, artist = display_song_artist(current.title, current.artist)
        query = f"{song_title} {artist}" if artist else song_title

    async with ctx.typing():
        song = await asyncio.to_thread(genius_client.search_song, query)

    if not song or not song.lyrics:
        await reply_embed(
            ctx,
            "Lyrics not found",
            "I could not find lyrics for that.",
            color=ERROR_COLOR,
        )
        return

    lyrics_text = song.lyrics.strip()
    lyrics_text = clamp_embed_text(lyrics_text, limit=3500)

    embed = make_embed(
        f"Lyrics: {song.title}",
        f"by **{song.artist}**\n\n{lyrics_text}",
    )
    await ctx.reply(embed=embed, mention_author=False)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        command_name = ctx.invoked_with or "that command"
        await reply_embed(
            ctx,
            "Unknown command",
            f"`{COMMAND_PREFIX}{command_name}` is not a command. Use `{COMMAND_PREFIX}help` to see the command list.",
            color=WARNING_COLOR,
        )
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await reply_embed(
            ctx,
            "Missing argument",
            f"Try `{COMMAND_PREFIX}help`.",
            color=WARNING_COLOR,
        )
        return
    logger.error("Command error: %r", error)
    await reply_embed(
        ctx,
        "Command failed",
        f"Something went wrong: {error}",
        color=ERROR_COLOR,
    )


def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to your .env file.")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
