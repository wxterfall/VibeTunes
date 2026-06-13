# VibeTunes, a simple Python Music Bot

A simple Discord music bot with text commands only.
Contains: `discord.py` voice playback, `yt-dlp` extraction, FFmpeg streaming, music-service metadata resolvers, and Genius lyrics.

## Features

- `!play <query or link>` queues a song or playlist.
- `!pause`, `!resume` pauses or resumes playback
- `!skip` skips the current track
- `!stop` stops playback and clears the queue
- `!disconnect` disconnects the bot from the voice channe;
- `!queue` shows the queue with interactive pages
- `!now` shows current track information and timeline
- `!loop` toggles looping for the current track
- `!shuffle` shuffles the queue
- `!volume` changes the bot playback volume between 0 and 200
- `!status` shows detailed bot status and performance stats
- `!lyrics [song]` fetches the lyrics for currently playing track or a given song
- `!syncedlyrics [song]` uses `syncedlyrics` package for timestamped LRC lyrics.
- Embed responses throughout the bot, including an interactive paged queue.


## Setup

1. Install Python 3.10+.
2. Install FFmpeg and make sure `ffmpeg` is available in your PATH.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in your keys:

```ini
DISCORD_TOKEN=your_discord_bot_token
COMMAND_PREFIX=!
BOT_NAME=VibeTunes
EMBED_COLOR=C026D3
NOW_PLAYING_UPDATE_SECONDS=10
IDLE_DISCONNECT_SECONDS=600
SYNCED_LYRICS_ENABLED=true
SYNCED_LYRICS_PROVIDERS=
SYNCED_LYRICS_CONTEXT_LINES=1
SYNCED_LYRICS_OFFSET_SECONDS=0
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_SCRAPER_BROWSER=requests
SPOTIFY_COOKIE_FILE=
SPOTIFY_COOKIE_HEADER=
SPOTIFY_SCRAPER_LOG_LEVEL=WARNING
GENIUS_TOKEN=your_genius_token
```

5. In the Discord Developer Portal, enable these bot intents:

- Server Members is not required.
- Message Content Intent is required.
- Guilds and Voice States are required.

6. Invite the bot with permissions for sending messages, connecting, and speaking.

7. Run it:

```bash
python bot.py
```

## Commands

| Command | Description |
| --- | --- |
| `!play <url/query>` | Queue a song, playlist, search, or supported music-service link. |
| `!pause` / `!resume` | Pause or resume playback. |
| `!skip` | Skip the current song. |
| `!stop` | Clear the queue and leave voice. |
| `!disconnect` | Leave voice and clear the queue. |
| `!queue` | Show the current queue with Previous/Next buttons. |
| `!now` | Show the current song with an updating timeline. |
| `!loop` | Toggle repeat for the current song. |
| `!volume <0-200>` | Set playback volume. |
| `!status` / `!stats` | Show detailed bot status and performance stats. |
| `!lyrics [song]` | Fetch lyrics using Genius. |
| `!syncedlyrics [song]` | Show synced lyrics for the current song or fetch timestamped lyrics for a query. |

## Notes

Spotify, Deezer, Apple Music, Tidal, and Amazon Music links do not play protected service audio directly. The bot reads metadata from those links, then searches for an equivalent playable source.

Spotify links are handled through SpotifyScraper first, with the Spotify API credentials used as a fallback when configured. Individual tracks, albums, public playlists, `spotify:...` URIs, `spotify.link` short links, and Spotify playlist-style curated mixes are supported. Personal/private playlists and personalized mixes can work when `SPOTIFY_COOKIE_FILE` points to an exported Spotify cookies.txt file or `SPOTIFY_COOKIE_HEADER` contains your Spotify browser cookie header.

Use `!syncedlyrics` while music is playing to open a separate updating synced-lyrics embed. Leave `SYNCED_LYRICS_PROVIDERS` blank to let `syncedlyrics` try its default providers, or set a comma-separated list such as `Lrclib,NetEase`. If lyrics are consistently early or late, tune `SYNCED_LYRICS_OFFSET_SECONDS`.

The bot automatically disconnects after `IDLE_DISCONNECT_SECONDS` of inactivity when it is connected to voice with nothing playing and nothing queued. Set it to `0` to disable the idle disconnect timer.

YouTube and other extractors can change or rate-limit over time. 
Self-hosting is more reliable than running a large public bot.
