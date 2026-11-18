#!/usr/bin/env python3
"""
clip-watcher GUI: Modern local web dashboard for monitoring Twitch streams,
detecting chat spikes, previewing vertical clips, and managing subtitles.
"""
import http.server
import json
import logging
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

PORT = 5000
DASHBOARD_HOST = "127.0.0.1"
SCRIPT_DIR = Path(__file__).resolve().parent
CLIPS_DIR = SCRIPT_DIR / "clips"
APP_LOG_PATH = SCRIPT_DIR / "clip-watcher.log"
MAX_REQUEST_BYTES = 64 * 1024
ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".srt"}
os.chdir(SCRIPT_DIR)

# Global watcher process management
watcher_proc = None
watcher_lock = threading.Lock()
log_history = []
max_logs = 300
chart_data = []  # list of {time, msgs, median, threshold}
current_status = {
    "state": "idle",  # idle, starting, warming_up, monitoring, spike, cutting, transcribing
    "channel": "",
    "started_at": None,
    "last_spike": None,
    "stats": {"msgs": 0, "median": 0.0, "threshold": 0.0},
    "error": None,
}


def _build_file_logger():
    """Create a bounded UTF-8 dashboard log without adding a dependency."""
    logger = logging.getLogger("clip_watcher.dashboard")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        handler = RotatingFileHandler(
            APP_LOG_PATH,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handler.handleError = lambda record: None
        logger.addHandler(handler)
    except OSError:
        # Logging must not prevent the local dashboard from starting.
        logger.addHandler(logging.NullHandler())
    return logger


file_logger = _build_file_logger()


def startup_messages(url):
    """Return an ASCII-only console banner safe for legacy Windows code pages."""
    return (
        "============================================================",
        "  clip-watcher Dashboard is running.",
        f"  Open in your browser: {url}",
        "============================================================",
    )


def append_log(message):
    """Keep bounded dashboard logs while holding watcher_lock at the call site."""
    text = str(message)
    log_history.append(text)
    if len(log_history) > max_logs:
        log_history.pop(0)
    try:
        file_logger.info(text)
    except Exception:
        # A full or unavailable disk must not stop stream monitoring.
        pass


def resolve_clip_path(filename):
    """Return a verified clip/subtitle path, or None for unsafe/missing input."""
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        return None
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        return None
    candidate = CLIPS_DIR / filename
    if candidate.suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS or candidate.is_symlink():
        return None
    try:
        resolved_root = CLIPS_DIR.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    if not resolved_candidate.is_file():
        return None
    return resolved_candidate


def log_listener(proc):
    """Read lines from watcher process and parse metrics in real time."""
    global watcher_proc, current_status
    for line in iter(proc.stdout.readline, ''):
        if not line:
            break
        line = line.strip()
        with watcher_lock:
            append_log(line)

            # Parse status and chat metrics
            # Example: [21:22:02] chat: 13 msgs / 10s median=0.0 threshold=5.0 (warming up)
            chat_m = re.search(r"chat:\s+(\d+)\s+msgs\s+/\s+\d+s\s+median=([\d\.]+)\s+threshold=([\d\.]+)(\s+\(warming up\))?", line)
            if chat_m:
                msgs = int(chat_m.group(1))
                median = float(chat_m.group(2))
                threshold = float(chat_m.group(3))
                is_warming = bool(chat_m.group(4))
                current_status["stats"] = {"msgs": msgs, "median": median, "threshold": threshold}
                current_status["state"] = "warming_up" if is_warming else "monitoring"
                now_str = datetime.now().strftime("%H:%M:%S")
                chart_data.append({"time": now_str, "msgs": msgs, "median": median, "threshold": threshold})
                if len(chart_data) > 30:
                    chart_data.pop(0)

            # Parse spike event
            spike_m = re.search(r"SPIKE!\s+(\d+)\s+msgs", line)
            if spike_m:
                current_status["state"] = "spike"
                current_status["last_spike"] = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "msgs": int(spike_m.group(1))
                }

            if "clip saved:" in line:
                current_status["state"] = "monitoring"
            elif "whisper: transcribing" in line:
                current_status["state"] = "transcribing"
            elif "recording started" in line and current_status["state"] == "starting":
                current_status["state"] = "warming_up"
            elif "ERROR:" in line:
                current_status["state"] = "error"
                current_status["error"] = line

    with watcher_lock:
        if watcher_proc == proc:
            watcher_proc = None
            exit_code = proc.poll()
            if exit_code not in (0, None):
                current_status["state"] = "error"
                current_status["error"] = f"Watcher exited with code {exit_code}."
                append_log(f"[Dashboard] Watcher failed with exit code {exit_code}.")
            else:
                current_status["state"] = "idle"
                append_log("[Dashboard] Watcher process terminated.")


def get_clips_list():
    """Scan clips/ directory and return list of metadata and subtitles."""
    if not CLIPS_DIR.exists():
        return []
    items = []
    for f in os.listdir(CLIPS_DIR):
        if f.lower().endswith(".mp4"):
            mp4_path = resolve_clip_path(f)
            if mp4_path is None:
                continue
            stat = mp4_path.stat()
            srt_name = os.path.splitext(f)[0] + ".srt"
            srt_path = resolve_clip_path(srt_name)
            has_srt = srt_path is not None
            srt_content = ""
            if has_srt:
                try:
                    with open(srt_path, "r", encoding="utf-8", errors="ignore") as srt_f:
                        srt_content = srt_f.read()
                except Exception:
                    pass
            items.append({
                "name": f,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp": stat.st_mtime,
                "has_srt": has_srt,
                "srt_name": srt_name if has_srt else None,
                "srt_preview": srt_content.strip()
            })
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return items


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP Request Handler supporting API endpoints and video range requests."""

    def log_message(self, format_string, *args):
        """Write HTTP diagnostics to the UTF-8 application log, not the console."""
        append_log("HTTP: " + (format_string % args))

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return

        if path == "/api/status":
            with watcher_lock:
                data = {
                    "running": watcher_proc is not None,
                    "status": current_status,
                    "logs": log_history[-100:],
                    "chart": chart_data
                }
            self.send_json(data)
            return

        if path == "/api/clips":
            clips = get_clips_list()
            self.send_json({"clips": clips})
            return

        if path.startswith("/clips/"):
            # Stream video / srt file
            filename = urllib.parse.unquote(path[len("/clips/"):])
            filepath = resolve_clip_path(filename)
            if filepath is None:
                self.send_error(404, "File not found")
                return

            if filepath.suffix.lower() == ".mp4":
                self.serve_video_range(filepath)
            elif filepath.suffix.lower() == ".srt":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(403, "Forbidden")
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": "Invalid Content-Length"}, status=400)
            return
        if content_len < 0 or content_len > MAX_REQUEST_BYTES:
            self.send_json({"error": "Request body is too large"}, status=413)
            return
        try:
            body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        except UnicodeDecodeError:
            self.send_json({"error": "Request body must be UTF-8"}, status=400)
            return
        try:
            req_data = json.loads(body)
        except Exception:
            self.send_json({"error": "Request body must be valid JSON"}, status=400)
            return
        if not isinstance(req_data, dict):
            self.send_json({"error": "Request body must be a JSON object"}, status=400)
            return

        if path == "/api/start":
            global watcher_proc, current_status
            with watcher_lock:
                if watcher_proc is not None:
                    self.send_json({"error": "Watcher is already running"}, status=400)
                    return

                channel_value = req_data.get("channel", "")
                if not isinstance(channel_value, str):
                    self.send_json({"error": "Channel must be text"}, status=400)
                    return
                channel = channel_value.strip().lstrip("#")
                if not channel or len(channel) > 2048 or "\x00" in channel:
                    self.send_json({"error": "Channel name is required"}, status=400)
                    return

                cmd = [sys.executable, "watcher.py", channel]
                min_msgs = req_data.get("min_msgs")
                if min_msgs: cmd.extend(["--min", str(min_msgs)])
                factor = req_data.get("factor")
                if factor: cmd.extend(["--factor", str(factor)])
                quality = req_data.get("quality", "best")
                if quality: cmd.extend(["--quality", quality])
                whisper_model = req_data.get("whisper_model", "base")
                if whisper_model: cmd.extend(["--whisper-model", whisper_model])
                if req_data.get("no_whisper"): cmd.append("--no-whisper")

                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=SCRIPT_DIR,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1
                    )
                    watcher_proc = proc
                    current_status = {
                        "state": "starting",
                        "channel": channel,
                        "started_at": datetime.now().strftime("%H:%M:%S"),
                        "last_spike": None,
                        "stats": {"msgs": 0, "median": 0.0, "threshold": 0.0},
                        "error": None,
                    }
                    chart_data.clear()
                    log_history.clear()
                    append_log(f"[Dashboard] Started watcher for #{channel}...")
                    threading.Thread(target=log_listener, args=(proc,), daemon=True).start()
                    self.send_json({"success": True, "channel": channel})
                except Exception as e:
                    self.send_json({"error": str(e)}, status=500)
            return

        if path == "/api/stop":
            with watcher_lock:
                if watcher_proc is not None:
                    watcher_proc.terminate()
                    try:
                        watcher_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        watcher_proc.kill()
                    watcher_proc = None
                    current_status["state"] = "idle"
                    current_status["error"] = None
                    append_log("[Dashboard] Watcher stopped by user.")
                    self.send_json({"success": True})
                else:
                    self.send_json({"error": "Watcher is not running"}, status=400)
            return

        if path == "/api/open-folder":
            CLIPS_DIR.mkdir(exist_ok=True)
            if sys.platform == "win32":
                os.startfile(CLIPS_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(CLIPS_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(CLIPS_DIR)])
            self.send_json({"success": True})
            return

        self.send_error(404, "Not Found")

    def send_json(self, data, status=200):
        res = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def serve_video_range(self, filepath):
        """Serve MP4 video with HTTP 206 Partial Content / Range support for smooth playback."""
        size = os.path.getsize(filepath)
        range_header = self.headers.get("Range")
        if range_header:
            range_match = re.search(r"bytes=(\d+)-(\d*)", range_header)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2)) if range_match.group(2) else size - 1
                if start >= size or end < start:
                    self.send_error(416, "Requested range not satisfiable")
                    return
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(filepath, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
                return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>clip-watcher Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎬</text></svg>">
<style>
  :root {
    --bg-primary: #0e0e10;
    --bg-secondary: #18181b;
    --bg-tertiary: #1f1f23;
    --accent: #9146ff;
    --accent-hover: #772ce8;
    --accent-glow: rgba(145, 70, 255, 0.35);
    --text-primary: #efeff1;
    --text-secondary: #adadb8;
    --border: #2d2d35;
    --success: #00f59b;
    --warning: #ffb300;
    --danger: #eb0400;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  body { background: var(--bg-primary); color: var(--text-primary); padding: 24px; min-height: 100vh; }
  .container { max-width: 1400px; margin: 0 auto; display: flex; flex-direction: column; gap: 24px; }

  /* Header */
  header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 18px; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand h1 { font-size: 24px; font-weight: 700; background: linear-gradient(135deg, #fff 0%, #adadb8 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .badge-status { display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px; border-radius: 999px; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .status-idle { background: rgba(173, 173, 184, 0.15); color: var(--text-secondary); }
  .status-running { background: rgba(0, 245, 155, 0.15); color: var(--success); }
  .status-spike { background: rgba(235, 4, 0, 0.2); color: var(--danger); animation: pulse 1s infinite alternate; }
  @keyframes pulse { from { opacity: 0.7; } to { opacity: 1; } }

  /* Grid */
  .grid-top { display: grid; grid-template-columns: 360px 1fr; gap: 24px; }
  @media (max-width: 900px) { .grid-top { grid-template-columns: 1fr; } }

  .card { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
  .card-title { font-size: 16px; font-weight: 600; display: flex; justify-content: space-between; align-items: center; color: #fff; }

  /* Controls Form */
  .form-group { display: flex; flex-direction: column; gap: 6px; }
  label { font-size: 13px; font-weight: 500; color: var(--text-secondary); }
  input[type="text"], input[type="number"], select { background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 8px; color: #fff; padding: 10px 12px; font-size: 14px; outline: none; transition: 0.2s border; }
  input:focus, select:focus { border-color: var(--accent); }
  .hint { font-size: 11px; color: var(--text-secondary); }
  .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

  .btn { display: inline-flex; justify-content: center; align-items: center; gap: 8px; padding: 12px 18px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; border: none; transition: 0.2s all; }
  .btn-primary { background: var(--accent); color: #fff; box-shadow: 0 4px 14px var(--accent-glow); }
  .btn-primary:hover { background: var(--accent-hover); transform: translateY(-1px); }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-danger:hover { opacity: 0.9; }
  .btn-secondary { background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border); }
  .btn-secondary:hover { background: #2a2a30; }

  /* Metrics & Chart */
  .metrics-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .metric-box { background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 8px; padding: 12px; text-align: center; }
  .metric-val { font-size: 26px; font-weight: 700; color: #fff; margin-top: 4px; }
  .metric-lbl { font-size: 11px; text-transform: uppercase; color: var(--text-secondary); font-weight: 600; }

  /* Mini Chart */
  .chart-container { width: 100%; height: 120px; background: var(--bg-tertiary); border-radius: 8px; border: 1px solid var(--border); position: relative; overflow: hidden; }
  svg.chart { width: 100%; height: 100%; }

  /* Console */
  .console { background: #000; border-radius: 8px; padding: 12px; height: 180px; overflow-y: auto; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; line-height: 1.5; color: #adadb8; border: 1px solid var(--border); }
  .console-line { white-space: pre-wrap; margin-bottom: 2px; }
  .console-line.spike { color: #ff5252; font-weight: bold; }
  .console-line.clip { color: #00f59b; font-weight: bold; }

  /* Clips Gallery */
  .clips-header { display: flex; justify-content: space-between; align-items: center; }
  .clips-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 18px; margin-top: 12px; }
  .clip-card { background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; transition: 0.2s transform, 0.2s border; cursor: pointer; }
  .clip-card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .clip-thumb { background: #0a0a0c; aspect-ratio: 9/16; max-height: 240px; display: flex; align-items: center; justify-content: center; position: relative; overflow: hidden; }
  .clip-thumb video { width: 100%; height: 100%; object-fit: cover; }
  .play-overlay { position: absolute; width: 44px; height: 44px; border-radius: 50%; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; color: #fff; font-size: 18px; pointer-events: none; }
  .clip-info { padding: 12px; display: flex; flex-direction: column; gap: 6px; }
  .clip-name { font-size: 13px; font-weight: 600; color: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .clip-meta { font-size: 11px; color: var(--text-secondary); display: flex; justify-content: space-between; }
  .clip-srt { font-size: 12px; color: #00f59b; background: rgba(0, 245, 155, 0.08); padding: 6px 8px; border-radius: 6px; margin-top: 4px; max-height: 44px; overflow: hidden; text-overflow: ellipsis; }

  /* Modal Video Player */
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 1000; align-items: center; justify-content: center; padding: 20px; backdrop-filter: blur(4px); }
  .modal.active { display: flex; }
  .modal-content { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 14px; max-width: 900px; width: 100%; max-height: 90vh; display: grid; grid-template-columns: 360px 1fr; overflow: hidden; position: relative; }
  @media (max-width: 768px) { .modal-content { grid-template-columns: 1fr; max-width: 420px; } }
  .modal-video-wrap { background: #000; aspect-ratio: 9/16; max-height: 75vh; display: flex; align-items: center; justify-content: center; }
  .modal-video-wrap video { width: 100%; height: 100%; object-fit: contain; }
  .modal-sidebar { padding: 20px; display: flex; flex-direction: column; gap: 14px; overflow-y: auto; }
  .modal-close { position: absolute; top: 12px; right: 12px; background: rgba(0,0,0,0.5); border: none; color: #fff; width: 32px; height: 32px; border-radius: 50%; cursor: pointer; font-size: 16px; z-index: 10; display: flex; align-items: center; justify-content: center; }
  .srt-box { background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-family: monospace; font-size: 12px; line-height: 1.6; white-space: pre-wrap; color: #adadb8; max-height: 350px; overflow-y: auto; }
</style>
</head>
<body>

<div class="container">
  <header>
    <div class="brand">
      <span style="font-size: 32px;">🎬</span>
      <div>
        <h1>clip-watcher</h1>
        <p style="font-size: 12px; color: var(--text-secondary);">Automated Twitch & YouTube Live Chat Spike Detector & 9:16 Vertical Video Clipper</p>
      </div>
    </div>
    <div id="statusBadge" class="badge-status status-idle">
      <span style="font-size: 10px;">●</span> <span id="statusText">IDLE</span>
    </div>
  </header>

  <div class="grid-top">
    <!-- Configuration & Control Form -->
    <div class="card">
      <div class="card-title">Stream Setup</div>
      
      <div class="form-group">
        <label for="channel">Twitch Channel or YouTube Live URL</label>
        <input type="text" id="channel" placeholder="e.g. shroud, or https://www.youtube.com/watch?v=..." value="fps_shaka">
        <span class="hint">Supports Twitch channels and YouTube Live stream URLs.</span>
      </div>

      <div class="row-2">
        <div class="form-group">
          <label for="minMsgs">Min Messages</label>
          <input type="number" id="minMsgs" value="10" min="1" max="100">
          <span class="hint">Quiet chats: 5</span>
        </div>
        <div class="form-group">
          <label for="factor">Spike Multiplier</label>
          <input type="number" id="factor" value="2.5" step="0.1" min="1.1" max="10">
          <span class="hint">Default: 2.5x</span>
        </div>
      </div>

      <div class="row-2">
        <div class="form-group">
          <label for="quality">Quality</label>
          <select id="quality">
            <option value="best" selected>Best (Source)</option>
            <option value="720p60">720p 60fps</option>
            <option value="480p30">480p 30fps</option>
            <option value="worst">Worst (Fastest)</option>
          </select>
        </div>
        <div class="form-group">
          <label for="whisperModel">Whisper Model</label>
          <select id="whisperModel">
            <option value="base" selected>Base (Fast)</option>
            <option value="tiny">Tiny (Lightest)</option>
            <option value="small">Small (Better)</option>
          </select>
        </div>
      </div>

      <div style="display: flex; align-items: center; gap: 8px;">
        <input type="checkbox" id="noWhisper">
        <label for="noWhisper" style="cursor: pointer;">Skip Subtitle Generation</label>
      </div>

      <div style="display: flex; gap: 10px; margin-top: 8px;">
        <button id="btnStart" class="btn btn-primary" style="flex: 1;" onclick="startWatcher()">▶ Start Watching</button>
        <button id="btnStop" class="btn btn-danger" style="display: none; flex: 1;" onclick="stopWatcher()">■ Stop</button>
      </div>
    </div>

    <!-- Live Telemetry & Console -->
    <div class="card">
      <div class="card-title">
        <span>Live Activity & Metrics</span>
        <span id="activeChannelBadge" style="font-size: 12px; color: var(--accent); font-weight: 600;"></span>
      </div>

      <div class="metrics-row">
        <div class="metric-box">
          <div class="metric-lbl">Chat / 10s</div>
          <div id="metricMsgs" class="metric-val">0</div>
        </div>
        <div class="metric-box">
          <div class="metric-lbl">3-Min Median</div>
          <div id="metricMedian" class="metric-val">0.0</div>
        </div>
        <div class="metric-box">
          <div class="metric-lbl">Spike Threshold</div>
          <div id="metricThreshold" class="metric-val">0.0</div>
        </div>
      </div>

      <div class="chart-container">
        <svg id="chartSvg" class="chart"></svg>
      </div>

      <div class="card-title" style="margin-top: 4px;">Live Log</div>
      <div id="consoleBox" class="console">
        <div class="console-line">[Ready] Enter channel and click Start Watching.</div>
      </div>
    </div>
  </div>

  <!-- Clips Gallery -->
  <div class="card">
    <div class="clips-header">
      <div class="card-title">Generated 9:16 Clips (<span id="clipsCount">0</span>)</div>
      <div style="display: flex; gap: 10px;">
        <button class="btn btn-secondary" onclick="loadClips()">↻ Refresh</button>
        <button class="btn btn-secondary" onclick="openFolder()">📂 Open Clips Folder</button>
      </div>
    </div>

    <div id="clipsGrid" class="clips-grid">
      <div style="color: var(--text-secondary); font-size: 13px; grid-column: 1/-1; text-align: center; padding: 40px;">No clips created yet. Start watching a stream to detect spikes!</div>
    </div>
  </div>
</div>

<!-- Video Preview Modal -->
<div id="videoModal" class="modal" onclick="closeModal(event)">
  <div class="modal-content" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <div class="modal-video-wrap">
      <video id="modalVideo" controls autoplay playsinline></video>
    </div>
    <div class="modal-sidebar">
      <h3 id="modalTitle" style="font-size: 16px; word-break: break-all;"></h3>
      <p id="modalMeta" style="font-size: 12px; color: var(--text-secondary);"></p>
      <div style="font-size: 12px; font-weight: 600; color: #fff; margin-top: 6px;">Whisper Subtitles (.srt)</div>
      <div id="modalSrt" class="srt-box">No subtitles available.</div>
    </div>
  </div>
</div>

<script>
let lastStatus = "";

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.parseJson ? await res.parseJson() : await res.json();
    updateUI(data);
  } catch (err) {
    console.error("fetch status error:", err);
  }
}

function updateUI(data) {
  const isRunning = data.running;
  const status = data.status;

  // Buttons toggle
  document.getElementById("btnStart").style.display = isRunning ? "none" : "flex";
  document.getElementById("btnStop").style.display = isRunning ? "flex" : "none";
  document.getElementById("channel").disabled = isRunning;
  document.getElementById("minMsgs").disabled = isRunning;
  document.getElementById("factor").disabled = isRunning;
  document.getElementById("quality").disabled = isRunning;
  document.getElementById("whisperModel").disabled = isRunning;
  document.getElementById("noWhisper").disabled = isRunning;

  // Status Badge
  const badge = document.getElementById("statusBadge");
  const badgeText = document.getElementById("statusText");
  badge.className = "badge-status";
  if (!isRunning) {
    badge.classList.add("status-idle");
    badgeText.textContent = "IDLE";
  } else if (status.state === "spike") {
    badge.classList.add("status-spike");
    badgeText.textContent = "SPIKE DETECTED";
  } else {
    badge.classList.add("status-running");
    badgeText.textContent = status.state.toUpperCase();
  }

  // Active channel badge
  document.getElementById("activeChannelBadge").textContent = isRunning ? `#${status.channel}` : "";

  // Metrics
  document.getElementById("metricMsgs").textContent = status.stats.msgs || 0;
  document.getElementById("metricMedian").textContent = (status.stats.median || 0).toFixed(1);
  document.getElementById("metricThreshold").textContent = (status.stats.threshold || 0).toFixed(1);

  // Render Chart
  renderChart(data.chart || []);

  // Logs
  const consoleBox = document.getElementById("consoleBox");
  const isScrolledToBottom = consoleBox.scrollHeight - consoleBox.clientHeight <= consoleBox.scrollTop + 20;
  consoleBox.innerHTML = (data.logs || []).map(line => {
    let cls = "console-line";
    if (line.includes("SPIKE!")) cls += " spike";
    else if (line.includes("clip saved:")) cls += " clip";
    return `<div class="${cls}">${escapeHtml(line)}</div>`;
  }).join("");
  if (isScrolledToBottom) {
    consoleBox.scrollTop = consoleBox.scrollHeight;
  }
}

function renderChart(points) {
  const svg = document.getElementById("chartSvg");
  if (!points || points.length === 0) {
    svg.innerHTML = "";
    return;
  }
  const w = svg.clientWidth || 600;
  const h = svg.clientHeight || 120;
  const pad = 12;

  const maxVal = Math.max(10, ...points.map(p => Math.max(p.msgs, p.threshold)));
  const stepX = (w - pad * 2) / Math.max(1, points.length - 1);

  const getPt = (idx, val) => {
    const x = pad + idx * stepX;
    const y = h - pad - ((val / maxVal) * (h - pad * 2));
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  };

  const msgsPts = points.map((p, i) => getPt(i, p.msgs)).join(" ");
  const threshPts = points.map((p, i) => getPt(i, p.threshold)).join(" ");

  svg.innerHTML = `
    <polyline fill="none" stroke="rgba(255, 179, 0, 0.4)" stroke-width="1.5" stroke-dasharray="4" points="${threshPts}" />
    <polyline fill="none" stroke="#9146ff" stroke-width="2.5" points="${msgsPts}" />
    ${points.map((p, i) => {
      const [cx, cy] = getPt(i, p.msgs).split(",");
      return `<circle cx="${cx}" cy="${cy}" r="3" fill="#00f59b" />`;
    }).join("")}
  `;
}

async function startWatcher() {
  const channel = document.getElementById("channel").value.trim();
  if (!channel) return alert("Please enter a Twitch channel name");
  const minMsgs = parseInt(document.getElementById("minMsgs").value) || 10;
  const factor = parseFloat(document.getElementById("factor").value) || 2.5;
  const quality = document.getElementById("quality").value;
  const whisperModel = document.getElementById("whisperModel").value;
  const noWhisper = document.getElementById("noWhisper").checked;

  try {
    const res = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel, min_msgs: minMsgs, factor, quality, whisper_model: whisperModel, no_whisper: noWhisper })
    });
    const d = await res.json();
    if (d.error) alert(d.error);
    else fetchStatus();
  } catch (err) {
    alert("Failed to start watcher: " + err);
  }
}

async function stopWatcher() {
  try {
    const res = await fetch("/api/stop", { method: "POST" });
    const d = await res.json();
    if (d.error) alert(d.error);
    else {
      fetchStatus();
      loadClips();
    }
  } catch (err) {
    alert("Failed to stop watcher: " + err);
  }
}

async function openFolder() {
  await fetch("/api/open-folder", { method: "POST" });
}

async function loadClips() {
  try {
    const res = await fetch("/api/clips");
    const d = await res.json();
    const clips = d.clips || [];
    document.getElementById("clipsCount").textContent = clips.length;
    const grid = document.getElementById("clipsGrid");
    if (clips.length === 0) {
      grid.innerHTML = '<div style="color: var(--text-secondary); font-size: 13px; grid-column: 1/-1; text-align: center; padding: 40px;">No clips created yet. Start watching a stream to detect spikes!</div>';
      return;
    }
    grid.innerHTML = clips.map(clip => `
      <div class="clip-card" onclick='openModal(${JSON.stringify(clip)})'>
        <div class="clip-thumb">
          <video src="/clips/${encodeURIComponent(clip.name)}" preload="metadata" muted></video>
          <div class="play-overlay">▶</div>
        </div>
        <div class="clip-info">
          <div class="clip-name" title="${escapeHtml(clip.name)}">${escapeHtml(clip.name)}</div>
          <div class="clip-meta">
            <span>${clip.size_mb} MB</span>
            <span>${clip.modified}</span>
          </div>
          ${clip.srt_preview ? `<div class="clip-srt">💬 ${escapeHtml(clip.srt_preview.replace(/\\d+\\n\\d\\d:\\d\\d:[\\d,]+ --> [\\d,:]+\\n/g, '').slice(0, 70))}...</div>` : ''}
        </div>
      </div>
    `).join("");
  } catch (err) {
    console.error("load clips err:", err);
  }
}

function openModal(clip) {
  document.getElementById("modalTitle").textContent = clip.name;
  document.getElementById("modalMeta").textContent = `${clip.size_mb} MB • ${clip.modified}`;
  const video = document.getElementById("modalVideo");
  video.src = "/clips/" + encodeURIComponent(clip.name);
  video.play();
  document.getElementById("modalSrt").textContent = clip.srt_preview || "No subtitles generated for this clip.";
  document.getElementById("videoModal").classList.add("active");
}

function closeModal() {
  const modal = document.getElementById("videoModal");
  modal.classList.remove("active");
  const video = document.getElementById("modalVideo");
  video.pause();
  video.src = "";
}

function escapeHtml(str) {
  return String(str || "").replace(/[&<>"']/g, m => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[m]));
}

// Polling interval
setInterval(fetchStatus, 1500);
fetchStatus();
loadClips();
</script>
</body>
</html>
"""

def main():
    try:
        server = ThreadedHTTPServer((DASHBOARD_HOST, PORT), DashboardHandler)
    except OSError as exc:
        append_log(f"ERROR: Dashboard could not bind to {DASHBOARD_HOST}:{PORT}: {exc}")
        print(f"ERROR: Dashboard could not bind to {DASHBOARD_HOST}:{PORT}: {exc}")
        sys.exit(1)
    url = f"http://localhost:{PORT}"
    append_log(f"Dashboard started on {DASHBOARD_HOST}:{PORT}")
    for message in startup_messages(url):
        print(message)
    try:
        webbrowser.open(url)
    except Exception as exc:
        append_log(f"Dashboard could not open a browser automatically: {exc}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
        append_log("Dashboard stopping after KeyboardInterrupt")
    finally:
        server.server_close()
        append_log("Dashboard stopped")


if __name__ == "__main__":
    main()
