# VibeTunes, a simple Python Music Bot

A simple Discord music bot with text commands only.
Contains: `discord.py` voice playback, `yt-dlp` extraction, FFmpeg streaming, Spotify-to-search conversion through the Spotify developer API, and Genius lyrics.

## Features

- `!play <query or link>` queues YouTube/SoundCloud/direct `yt-dlp` sources.
- `!play <spotify url>` converts Spotify tracks, albums, playlists, or artist
  top tracks into playable searches.
- `!pause`, `!resume`, `!skip`, `!stop`, `!disconnect`, `!queue`, `!now`,
  `!loop`, `!shuffle` and `!volume`.
- Embed responses throughout the bot, including an interactive paged queue.
- `!lyrics [song]` uses Genius when `GENIUS_TOKEN` is configured.
- `!syncedlyrics [song]` uses `syncedlyrics` package for timestamped LRC lyrics.


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
| `!play <url/query>` | Queue a song, playlist, search, or Spotify link. |
| `!pause` / `!resume` | Pause or resume playback. |
| `!skip` | Skip the current song. |
| `!stop` | Clear the queue and leave voice. |
| `!disconnect` | Leave voice and clear the queue. |
| `!queue` | Show the current queue with Previous/Next buttons. |
| `!now` | Show the current song with an updating timeline. |
| `!loop` | Toggle repeat for the current song. |
| `!volume <0-200>` | Set playback volume. |
| `!lyrics [song]` | Fetch lyrics using Genius. |
| `!syncedlyrics [song]` | Show synced lyrics for the current song or fetch timestamped lyrics for a query. |

## Notes

Spotify links do not play audio directly. 
The bot uses Spotify metadata to search for an equivalent playable source, which is the same practical approach many Discord music bots use.

Use `!syncedlyrics` while music is playing to open a separate updating synced-lyrics embed. Leave `SYNCED_LYRICS_PROVIDERS` blank to let `syncedlyrics` try its default providers, or set a comma-separated list such as `Lrclib,NetEase`. If lyrics are consistently early or late, tune `SYNCED_LYRICS_OFFSET_SECONDS`.

The bot automatically disconnects after `IDLE_DISCONNECT_SECONDS` of inactivity when it is connected to voice with nothing playing and nothing queued. Set it to `0` to disable the idle disconnect timer.

YouTube and other extractors can change or rate-limit over time. 
Self-hosting is more reliable than running a large public bot.
