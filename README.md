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

## Diagnosing Apple Music queue integration (v0.4.0)

The official Apple Music Windows app has no supported public Up Next queue API.
Windows UI Automation is experimental, varies across app versions/window layouts,
and may fail even when the song is already visible. **Never assume a song was added
unless Spindle verifies it in Apple's actual Playing Next list.**

To avoid further blind selector changes:

1. In the latest Spindle Windows preview, look for **v0.4.0 DIAGNOSTICS**
   in the lower-right footer.
2. Position Apple Music the way you normally stream (including a separate
   monitor, maximized if preferred) with Search **closed**.
3. Click **Inspect Apple Music** in Spindle's Activity card. It saves a
   read-only JSON file and opens the diagnostics folder.
4. Open Search **manually** in Apple Music and collect a second file.
5. If useful, open a song's **More (...)** menu, then collect a third file.
   This step is read-only on Spindle's side; the menu is opened manually.
6. Share the JSON files with the developer. The reports show the real
   accessibility control IDs, positions, menu patterns, and monitor geometry,
   allowing precise selectors to replace guesses.

Reports are saved under `%LOCALAPPDATA%\\Spindle\\diagnostics`. They are
created **only when you click Inspect Apple Music**. They are never uploaded by
Spindle. Music titles, artist names, account names, and playlist names are
redacted; review the files yourself before sharing.

The app's activity log now identifies whether insertion failed while reading
the queue, finding the song, opening More, choosing Play Next/Last, or verifying
the result. This is particularly helpful for COM failures on multi-monitor
setups. UIA snapshots and CI tests do **not** establish real-world insertion
success: that still requires testing in a signed-in Apple Music instance.


## Automatic Twitch requests (v0.5 preview)

`!sr needy Ariana Grande` creates a durable pending request immediately.
Spindle then attempts Apple Music delivery on a background worker **in the
order requests arrived**. The broadcaster does not have to approve or click
Queue for each request. The optional **Retry** button is for diagnostics only.

Spindle will not claim that a request is in Playing Next until it can verify
a matching title and artist in the official Apple Music Windows application.
Failed delivery is retried with increasing backoff while the app is running;
Spindle does not forcibly skip earlier requests or start playback of a new
track. It uses Apple's queue actions to preserve the current playlist.

**Important technical constraints:**

- Apple Music for Windows exposes no supported native queue API. Spindle uses
  Windows UI Automation, whose behavior changes across app versions.
- Filling an `AutoSuggestBox` displays suggestions (usually a few items) but
  **does not submit a full search**. Spindle explicitly tries the QueryButton,
  then app-scoped background Enter and confirms the results page changed.
  Old search results and the suggestion list are never mistaken for a new
  full-results page.
- When minimized, Apple Music can be temporarily restored using Windows'
  `SW_SHOWNOACTIVATE`, then returned to minimized state afterward.
- The **optional** Settings checkbox **Allow brief focus handoff** is off by
  default. If the app ignores the non-activating search path, this mode can
  briefly focus Apple Music to use its documented `Alt, N, F` Search access
  keys and Enter. **This can briefly interrupt a game.** The fallback refuses
  to send keyboard input unless Apple Music is verified as the foreground app.
- If neither accessible controls nor the chosen input mode work, delivery
  remains pending. No false success, no song skipped, and no requirement to
  manually approve each request.

Test with the official Microsoft Store app: play a song from a playlist,
minimize Apple Music, issue `!sr` from Twitch, and verify the next track and
subsequent return to the playlist. GitHub Actions cannot verify these live
Windows player interactions; only a signed-in workstation can.


## Game-safe queueing and broadcaster privileges (v0.5.3)

**The authenticated Twitch broadcaster has unlimited unique song requests**:
there is no request cooldown, no per-viewer song count limit, and no global
queue-count limit for the broadcaster. The same exemption applies to the
streamer's local **Find a song** action. Viewers and moderators still follow
the configured queue and cooldown limits. Repeated requests for the exact
same catalog track are still rejected to prevent unintended duplicates.
Spindle identifies the stream owner using the authenticated Twitch user ID,
not the displayed Twitch name.

In **Settings**, the default **Keep games and other apps in focus** option
uses background-only Windows UI Automation and app-scoped messages. It never
intentionally calls SetForegroundWindow, sends global key strokes or focuses
Apple Music's search box. When Apple Music was minimized, Spindle may briefly
restore it **without activation** and return it to its previous state.
This best-effort mode can still fail if Apple Music itself demands focus for
search submission; failed requests remain pending and are retried without
streamer approval.

For Apple Music versions that require a real focused Enter key, the separate
**Compatibility mode** option remains available. That mode can temporarily
take keyboard focus away from a full-screen game. These modes are mutually
exclusive. Any saved focus-handoff preference from earlier versions is
overridden by the new default game-safe setting until the streamer explicitly
selects compatibility.

**Important:** The official Apple Music Windows application has no supported
public native Up Next queue-control API. Background queueing is not guaranteed
on every installation. The actual end-to-end test is a viewer !sr while
Marvel Rivals (or another game) remains foreground: confirm both that the
request appears in Apple's Playing Next and that the game never loses focus.
An unattended request may remain pending in game-safe mode on an app build
that only accepts a foreground search submission.
