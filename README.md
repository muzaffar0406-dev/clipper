# clip-watcher

> Record a live Twitch stream, detect chat spikes over IRC, automatically cut 9:16 vertical clips (TikTok/Shorts/Reels ready), and generate Whisper `.srt` subtitles.

## Project layout and local safety

Run the application from this directory, which is the canonical source root. The adjacent `clip-watcher/` directory is currently a verified duplicate retained for safety; it is not the active source tree and should not be edited independently.

The dashboard listens only on `http://127.0.0.1:5000`. It is intentionally local-only and does not provide remote access.

---

## 🌐 Supported Platforms

| Platform | Input Format | Chat Method |
|---|---|---|
| **Twitch** | Channel name (`shroud`, `tarik`, `fps_shaka`) | Anonymous Twitch IRC (`irc.chat.twitch.tv:6667`) |
| **YouTube Live** | Full URL (`https://www.youtube.com/watch?v=...`), channel live link (`https://www.youtube.com/@channel/live`), or 11-char video ID | Real-time YouTube Live Chat API (`pytchat`) |

---

## 🖥️ Graphical User Interface (GUI)

A modern, dark-themed web dashboard is included for one-click controls, live chat spike charts, and 9:16 vertical video playback with subtitles:

- **Windows (Double-Click)**:
  Double-click `gui.bat` in Windows Explorer (or run `gui.bat` in CMD).
- **PowerShell**:
  ```powershell
  .\gui.ps1
  ```
- **Git Bash / macOS / Linux**:
  ```bash
  ./gui.sh
  ```
- **Direct Python**:
  ```bash
  python gui.py
  ```

This automatically opens **`http://localhost:5000`** in your default browser, providing:
1. **Interactive Controls**: Enter any Twitch channel, adjust sliders for `--min` and `--factor`, select video quality, and toggle Whisper.
2. **Real-time Live Activity**: Live message count per 10s, dynamic 3-minute median graph, and real-time watcher logs.
3. **Clips Gallery**: View all generated clips, play 9:16 vertical video right in your browser, and inspect Whisper subtitles.
4. **One-Click Explorer**: Open the `clips/` folder in Windows File Explorer directly from the UI.

---

## ⚡ CLI Usage (One-Command)

- **Linux / macOS / Git Bash**:
  ```bash
  ./watch.sh <twitch_channel>
  ```
- **Windows PowerShell**:
  ```powershell
  .\watch.ps1 <twitch_channel>
  ```
- **Windows CMD**:
  ```cmd
  watch.bat <twitch_channel>
  ```
- **Direct Python**:
  ```bash
  python watcher.py <twitch_channel>
  ```

---

## 🔧 How It Works

1. **Recording (`rec/`)**:
   Uses `streamlink` with low-latency HLS to record the live stream continuously into `rec/<channel>_<timestamp>.ts`.
2. **Anonymous Chat Reader (IRC)**:
   Connects to Twitch IRC anonymously (`irc.chat.twitch.tv:6667`, `NICK justinfan12345`, no OAuth token needed) and listens to channel chat.
3. **Spike Detection**:
   Counts chat messages in **10-second buckets**.
   A spike is detected when:
   $$\text{Bucket Count} \ge \max(\text{min\_msgs}, 2.5 \times \text{median of past 3 minutes})$$
   *(Default: factor 2.5x, 3-minute baseline window, min 10 messages)*.
4. **Vertical 9:16 Clipping (`clips/`)**:
   For each detected spike, `ffmpeg` cuts **20 seconds before + 10 seconds after** the spike (30s total), center-cropped to `9:16` vertical video (`1080x1920`), encoded with `libx264` (`yuv420p` + `setsar=1`) and `aac` audio for playback compatibility across all mobile and web players.
5. **Whisper Subtitles (`.srt`)**:
   OpenAI Whisper transcribes the clip and generates a matching `.srt` subtitle file with exact timestamps for each clip.

---

## Recording retention

To prevent continuous recordings from consuming unlimited storage, completed `.ts` files in `rec/` are cleaned before a recording starts and after the recorder stops. Clips and `.srt` subtitle files are never deleted by this policy.

Default policy:

- Keep recordings for at most 168 hours (7 days).
- Keep at most 20 GB of completed recordings.
- Preserve the active recording and files modified during the most recent five minutes.
- Remove eligible recordings oldest first, with every action written to the application log.

Optional environment variables:

| Variable | Default | Description |
|---|---:|---|
| `CLIP_WATCHER_CLEANUP_ENABLED` | `true` | Set to `false` to disable automatic recording cleanup. |
| `CLIP_WATCHER_RECORDING_RETENTION_HOURS` | `168` | Positive maximum recording age in hours. |
| `CLIP_WATCHER_MAX_RECORDING_GB` | `20` | Positive maximum total size for completed recordings. |

Invalid retention values stop the watcher with a clear error rather than applying an unsafe policy.

---

## 📝 Logging and Reliability

### Persistent File Logging
Both the dashboard (`gui.py`) and standalone CLI (`watcher.py`) write application and lifecycle events to a shared, persistent log:

- **Location**: `clip-watcher.log` in the application root directory.
- **Rotation Policy**: Bounded `RotatingFileHandler` with a maximum size of **1 MB** (1,000,000 bytes) and **3 backup files** (`clip-watcher.log.1`, `clip-watcher.log.2`, `clip-watcher.log.3`), encoded in UTF-8.
- **Fail-Safe Operation**: Any logging errors (such as disk full or filesystem permission errors) are safely caught and silenced so they never interrupt stream recording, clipping, or subtitle transcription.

### Windows CP1252-Safe Startup & Port Handling
- **CP1252 Console Safety**: All startup banners and console logging avoid emojis or non-ASCII characters to prevent `UnicodeEncodeError` on Windows consoles running legacy code pages (e.g., CP1252 in CMD or default PowerShell).
- **Port Conflict Handling**: If the dashboard port (default `5000`) is already bound by another process, `gui.py` records an error in `clip-watcher.log`, prints a clean error message, and exits with status code `1` rather than throwing an unhandled exception.

---

## Tests

The Stage 1 suite uses Python's built-in `unittest` module and does not require an additional test package:

```powershell
.\clip-watcher\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

---

## 💡 Practical Tips & Options

| Option | Default | Description |
|---|---|---|
| `--min N` | `10` | Minimum messages in a bucket to trigger a spike. **Tip: For quiet chats, use `--min 5`**. |
| `--factor F` | `2.5` | Spike multiplier over the 3-minute median. |
| `--bucket S` | `10` | Bucket length in seconds. |
| `--window S` | `180` | Moving median baseline window (seconds, 180s = 3 mins). |
| `--pre S` | `20` | Seconds to capture before the chat spike. |
| `--post S` | `10` | Seconds to capture after the chat spike. |
| `--cooldown S` | `30` | Minimum seconds between clips to avoid duplicate cuts. |
| `--quality Q` | `best` | Streamlink video quality (`best`, `720p60`, `480p30`, etc.). |
| `--whisper-model M` | `base` | Whisper model (`tiny`, `base`, `small`, `medium`). |
| `--no-whisper` | `False` | Disable Whisper subtitle generation. |

> [!NOTE]
> - **Live only**: The channel must be live when running the watcher.
> - **Streamer consent**: Always respect streamers' clip and content reuse permissions.
