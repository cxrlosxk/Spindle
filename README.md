# Spindle

**Your music. Their requests. One place to manage both.**

Spindle is an early-preview Windows companion for Twitch streamers who listen in the **official Apple Music app from the Microsoft Store**. It provides an understated desktop dashboard, accurate now-playing detection, live Twitch chat commands, and a persistent viewer-request queue.

> **Important limitation:** Spindle does **not** insert songs into Apple Music's native Up Next queue. Apple has not published a supported Windows API for that operation. Accepted `!sr` requests go into **Spindle's own pending queue** for manual opening and playback. The bot explicitly says *pending*, not *added to Apple Music*. We will only enable auto-queueing after verifying a reliable and non-disruptive implementation on Windows.

## What's built

- **`!song` / `!np`** — reads the title and artist currently published by Apple Music to Windows media sessions; reports paused/unavailable accurately.
- **`!sr AMEN Beyonce`** — looks up songs in Apple's public iTunes catalog, tolerates accent differences and avoids ambiguous matches. Direct Apple Music **song** links are also supported. Matches are not guarantees of Apple Music subscription availability.
- **`!queue`** — preview the first 3 queued requests.
- **`!unrequest`** — a viewer can cancel their most recent pending request.
- **`!sr on` / `!sr off`** — moderator-only request toggle.
- **`!remove 2`** — moderator-only removal by pending queue position.
- **Desktop queue tools** — manual request input, open the song link, mark a request played, or remove it.
- **Safety controls** — queue size, duplicate rejection, per-viewer cap, cooldown, max duration, and moderator-only commands.
- **Private by default** — SQLite queue and preferences are stored in `%LOCALAPPDATA%\Spindle`; Twitch OAuth tokens live in Windows Credential Manager through `keyring`.

## Requirements

- Windows 10 (recent build) or Windows 11, with the official Apple Music app installed.
- Python 3.12 (for the source-run preview); a Windows installer is planned.
- A Twitch account and **your own Twitch developer application** registered as a **Public Client** (only needed for chat integration).
- Apple Music playback must expose a Windows system media session for `!song` to work. This can depend on the installed app version.

## Run on Windows

1. Download the repository using **Code → Download ZIP**, extract it, and install [Python 3.12](https://www.python.org/downloads/) with the Python launcher.
2. Double-click `run_windows.bat`. It makes a local virtual environment and installs requirements.
3. Play a track in the official Apple Music app. Spindle should show its current title and artist if Windows publishes the session.
4. For Twitch, visit the [Twitch Developer Console](https://dev.twitch.tv/console/apps), create a **Public Client** application, and paste its **Client ID** into **Settings** (never a client secret).
5. Click **Connect Twitch**, open the login link, enter the displayed device code, and authorize the app. The bot responds as your own Twitch account and uses Twitch's EventSub WebSocket and Send Chat Message API. **Do not configure the same `!sr` command in Nightbot/StreamElements** or your audience will see duplicate replies.
6. Test `!song`, `!sr AMEN Beyonce`, and `!queue` in your Twitch chat. In Spindle, click **Open** beside a request to open its Apple Music / iTunes listing. Start playback manually and then mark it played with ✓.

For development, use `python -m pip install -r requirements-dev.txt` and `python -m pytest -q`.

## Privacy and security

No Twitch tokens or credentials belong in your repository, in settings JSON, or in chat messages. Tokens are stored through your operating system's credential manager. All catalog queries go to Apple's public search endpoint; Twitch account and chat operations go to Twitch. Song requests and queue metadata stay on your computer. Spindle doesn't download music or rebroadcast audio.

## Roadmap

1. Validate Apple Music media-session reliability on multiple Windows builds, and improve session selection and album art.
2. Add a verified, focus-safe queue handoff if the Windows app exposes a reliable route. Until then the queue remains explicitly manual.
3. Create an OBS browser overlay, a signed packaged Windows installer, and optional independent bot-account support.
4. Explore optional interoperability with Tonearm without coupling the projects.

## Current status

**Developer preview / unverified on a live Windows machine.** Pure command, catalog, and persistence tests can run on other operating systems, but the Windows UI, Apple Music integration, and Twitch authorization must be validated on Windows before a production release.

This project is not affiliated with or endorsed by Apple or Twitch. Music subscriptions do not grant broadcasting rights; streamer remains responsible for music licensing and platform rules.
