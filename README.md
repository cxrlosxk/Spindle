# Spindle

**Your music. Their requests. One place to manage both.**

Spindle is an early-preview Windows companion for Twitch streamers who listen in the **official Apple Music app from the Microsoft Store**. It provides an understated desktop dashboard, accurate now-playing detection, live Twitch chat commands, and a persistent viewer-request queue.

**Experimental native queue bridge (opt-in):** Spindle now attempts to control the official Windows app's **Play Next** and **Play Last** menus with Windows UI Automation, without simulated mouse or keyboard actions. It marks a request **Added to Up Next** only after detecting an additional matching entry in Apple Music's Playing Next panel. This cannot be fully verified in CI, which has no signed-in Apple Music installation. Some Apple Music Windows versions do not honor queue operations, and very long or virtualized queues may prevent reliable verification. Default remains **off** until confirmed on a real PC.

## What's built

- **`!song` / `!np`** — reads the title and artist currently published by Apple Music to Windows media sessions; reports paused/unavailable accurately.
- **`!sr AMEN Beyonce`** — looks up songs in Apple's public iTunes catalog, tolerates accent differences and avoids ambiguous matches. With native sync enabled, it attempts to queue the selected song in the *official Windows app*. Direct Apple Music **song** links are also supported. A catalog match does not prove subscription playback availability.
- **`!queue`** — preview Spindle requests and the count verified in Apple Music.
- **`!unrequest`** — a viewer can cancel their most recent pending request.
- **`!sr on` / `!sr off`** — moderator-only request toggle.
- **`!remove 2`** — moderator-only removal by pending queue position.
- **Desktop queue tools** — manual request input, open the song link, **Queue** button for pending requests (including older ones), mark a request played, or remove it.
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
6. First, test your existing queue in Spindle by clicking **Queue** next to a pending song while Apple Music is open, not minimized, and playing. Verify the track really appears in **Apple Music → Playing Next**. The Activity panel explains failed verification. The **Queue** button is an explicit manual attempt and does not require auto mode.
7. When that works, enable **Settings → Experimental: add requests to Apple Music Playing Next automatically**. Test `!sr AMEN Beyonce` with another song, followed by `!queue`. The first incoming Spindle request uses **Play Next**, and subsequent requests use **Play Last** to preserve submission order; existing album/playlist tracks may still be ahead of later requests.
8. If a request cannot be verified, **Spindle keeps it pending** and never claims it was added. Clicking **Open** still provides the manual handoff. Keep the Apple Music window open and not minimized for accessibility. Disable auto mode if the app interferes with gaming focus.

For development, use `python -m pip install -r requirements-dev.txt` and `python -m pytest -q`.

## Privacy and security

No Twitch tokens or credentials belong in your repository, in settings JSON, or in chat messages. Tokens are stored through your operating system's credential manager. All catalog queries go to Apple's public search endpoint; Twitch account and chat operations go to Twitch. Song requests and queue metadata stay on your computer. Spindle doesn't download music or rebroadcast audio.

## Roadmap

1. Validate Apple Music media-session reliability on multiple Windows builds, and improve session selection and album art.
2. Validate and harden the experimental native Up Next bridge on actual Windows devices, account for localized menus, window focus, queue virtualization, and mixed playlist/song request ordering.
3. Create an OBS browser overlay, a signed packaged Windows installer, and optional independent bot-account support.
4. Explore optional interoperability with Tonearm without coupling the projects.

## Current status

**Developer preview.** Twitch commands, Windows now-playing detection, and the local request queue have been demonstrated on the streamer's PC. The **native Up Next insertion introduced here remains unverified on that PC**; automated unit tests use a simulated native adapter and must not be mistaken for a successful real Apple Music insertion. UI Automation operates on Apple Music's own interface and has no supported contract from Apple. Test with a noncritical music queue before streaming. Never report `Added to Apple Music` without confirming a real new Up Next row. The product does not manipulate the audio stream itself.

### Native queue implementation references

- [Apple's Playing Next guide for Windows](https://support.apple.com/en-gw/guide/music-windows/musb1e6d1c76/windows) documents Play Next / Play Last.
- [Apple Music Remote for Windows](https://github.com/ahmetberber/windows-apple-music-remote) (MIT) demonstrates the relevant Windows accessibility IDs, UIA-based search, song menu invocation, and Up Next reading. Spindle's bridge is independent code informed by these techniques.
- [Python UIAutomation for Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) supports value and invoke patterns without mouse or keyboard injection.

This project is not affiliated with or endorsed by Apple or Twitch. Music subscriptions do not grant broadcasting rights; streamer remains responsible for music licensing and platform rules.
