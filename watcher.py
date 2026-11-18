#!/usr/bin/env python3
"""
clip-watcher: Record live Twitch or YouTube streams, detect chat spikes,
cut 9:16 vertical clips, and generate Whisper subtitles.
"""
import argparse
import logging
import os
import queue
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_config import ConfigurationError, load_retention_settings
from recording_manager import cleanup_recordings

# ---------------------------------------------------------------- Windows / SAC compatibility
# Preemptively handle Windows Smart App Control (SAC) blocking unsigned pycryptodome native libraries
try:
    import Crypto.Cipher.AES  # noqa: F401
except (ImportError, OSError):
    import types
    import hashlib

    class MockAES:
        block_size = 16
        MODE_CBC = 2
        MODE_ECB = 1
        @staticmethod
        def new(key, mode, iv=None):
            raise NotImplementedError("Encrypted streams not supported under Smart App Control")

    crypto_mod = types.ModuleType("Crypto")
    cipher_mod = types.ModuleType("Crypto.Cipher")
    cipher_mod.AES = MockAES
    cipher_mod.PKCS1_v1_5 = None
    crypto_mod.Cipher = cipher_mod

    hash_mod = types.ModuleType("Crypto.Hash")
    class MD5:
        @staticmethod
        def new(data=b""): return hashlib.md5(data)
    class SHA256:
        @staticmethod
        def new(data=b""): return hashlib.sha256(data)
    hash_mod.MD5 = MD5
    hash_mod.SHA256 = SHA256
    crypto_mod.Hash = hash_mod

    util_mod = types.ModuleType("Crypto.Util")
    padding_mod = types.ModuleType("Crypto.Util.Padding")
    def unpad(padded_data, block_size):
        p = padded_data[-1]
        return padded_data[:-p]
    def pad(data, block_size):
        p = block_size - (len(data) % block_size)
        return data + bytes([p] * p)
    padding_mod.unpad = unpad
    padding_mod.pad = pad
    util_mod.Padding = padding_mod
    crypto_mod.Util = util_mod

    pub_mod = types.ModuleType("Crypto.PublicKey")
    pub_mod.RSA = None
    crypto_mod.PublicKey = pub_mod

    sys.modules["Crypto"] = crypto_mod
    sys.modules["Crypto.Cipher"] = cipher_mod
    sys.modules["Crypto.Hash"] = hash_mod
    sys.modules["Crypto.Util"] = util_mod
    sys.modules["Crypto.Util.Padding"] = padding_mod
    sys.modules["Crypto.PublicKey"] = pub_mod


IRC_HOST, IRC_PORT, IRC_NICK = "irc.chat.twitch.tv", 6667, "justinfan12345"

stopping = threading.Event()
count_lock = threading.Lock()
msg_count = 0
PROJECT_DIR = Path(__file__).resolve().parent
APP_LOG_PATH = PROJECT_DIR / "clip-watcher.log"
FFMPEG_CUT_TIMEOUT_SECONDS = 180


def _build_file_logger():
    """Create a bounded UTF-8 logger for watcher without adding external dependencies."""
    logger = logging.getLogger("clip_watcher.watcher")
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
        logger.addHandler(logging.NullHandler())
    return logger


file_logger = _build_file_logger()


def log(msg):
    text = str(msg)
    try:
        print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)
    except Exception:
        pass
    try:
        file_logger.info(text)
    except Exception:
        # Logging failures (disk full, permission denied, etc.) must never interrupt stream recording or clipping.
        pass


def log_error(message):
    log(f"ERROR: {message}")


def setup_path():
    """Ensure ffmpeg, ffprobe, streamlink, and python venvs are discovered in PATH."""
    search_dirs = [
        os.path.abspath("clip-watcher/.venv/Scripts"),
        os.path.abspath(".venv/Scripts"),
        os.path.abspath("clip-watcher/.venv/bin"),
        os.path.abspath(".venv/bin"),
        os.path.abspath("ffmpeg/ffmpeg-9.0.2-essentials_build/bin"),
        os.path.abspath("../ffmpeg/ffmpeg-9.0.2-essentials_build/bin"),
    ]
    # Scan current directory tree for any ffmpeg binary folder
    for root, dirs, files in os.walk(os.path.abspath(".")):
        if "ffmpeg.exe" in files or "ffmpeg" in files:
            search_dirs.append(root)
            break

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    for d in search_dirs:
        if os.path.isdir(d) and d not in path_entries:
            path_entries.insert(0, d)
    os.environ["PATH"] = os.pathsep.join(path_entries)


def resolve_streamlink_command():
    """Returns the best command to invoke Streamlink."""
    if shutil.which("streamlink"):
        return ["streamlink"]
    return [sys.executable, "-m", "streamlink_cli"]


def relay_process_output(process, label):
    """Forward a child process's diagnostic output into the dashboard log."""
    if process.stdout is None:
        return
    try:
        for line in iter(process.stdout.readline, ""):
            if not line:
                break
            log(f"{label}: {line.rstrip()}")
    except Exception as exc:
        log_error(f"could not read {label} output: {exc}")


def build_ffmpeg_command(src, start, duration, output, *, accurate_seek=False):
    """Build the existing vertical clip command in a testable form."""
    vf_filter = "crop='trunc(ih*9/16/2)*2':ih,scale=1080:1920,setsar=1"
    seek = ["-ss", f"{start:.2f}"]
    input_args = ["-i", str(src)]
    if accurate_seek:
        input_and_seek = input_args + seek
    else:
        input_and_seek = seek + input_args
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        *input_and_seek, "-t", f"{duration:.2f}",
        "-vf", vf_filter,
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
    ]


def is_valid_clip_output(output):
    """Reject empty FFmpeg outputs without trusting only the process exit code."""
    try:
        return os.path.isfile(output) and os.path.getsize(output) > 10000
    except OSError:
        return False


def run_ffmpeg_cut(src, start, duration, output, *, accurate_seek=False):
    command = build_ffmpeg_command(src, start, duration, output, accurate_seek=accurate_seek)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFMPEG_CUT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log_error(f"FFmpeg timed out after {FFMPEG_CUT_TIMEOUT_SECONDS}s for {output}")
        return False
    except OSError as exc:
        log_error(f"FFmpeg could not start for {output}: {exc}")
        return False
    if completed.returncode == 0 and is_valid_clip_output(output):
        return True
    details = (completed.stderr or completed.stdout or "no FFmpeg output").strip()
    log_error(f"FFmpeg failed for {output} (exit {completed.returncode}): {details[:500]}")
    return False


def resolve_youtube_live_id(target):
    """Resolve an 11-char YouTube video ID from a watch URL, youtu.be, or @channel/live URL."""
    target = target.strip()
    # 1. Direct 11-char video ID
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", target):
        return target
    # 2. Standard URL containing video ID
    m = re.search(r"(?:v=|\/live\/|\/embed\/|youtu\.be\/)([a-zA-Z0-9_-]{11})", target)
    if m:
        return m.group(1)
    # 3. Channel URL like https://www.youtube.com/@channel/live
    if "youtube.com" in target or "youtu.be" in target:
        try:
            req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                m2 = re.search(r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})"', html)
                if m2:
                    return m2.group(1)
                m3 = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
                if m3:
                    return m3.group(1)
        except Exception as e:
            log(f"youtube: resolving live id error: {e}")
    return None


def parse_target(target):
    """Detect whether target is YouTube or Twitch and return (platform, identifier, streamlink_url)."""
    target = target.strip()
    is_yt = (
        "youtube.com" in target.lower()
        or "youtu.be" in target.lower()
        or (len(target) == 11 and re.fullmatch(r"[a-zA-Z0-9_-]{11}", target) and not target.isalpha())
    )
    if is_yt:
        vid = resolve_youtube_live_id(target)
        if vid:
            return ("youtube", vid, f"https://www.youtube.com/watch?v={vid}")
        # Fallback to direct URL if resolving wasn't possible
        clean_name = re.sub(r"[^\w\-]", "_", target.split("/")[-1] or "youtube_stream")
        return ("youtube", clean_name, target)

    # Otherwise default to Twitch
    channel = target.lstrip("#").lower()
    if channel.startswith("twitch.tv/"):
        channel = channel[len("twitch.tv/"):]
    return ("twitch", channel, f"twitch.tv/{channel}")


# ---------------------------------------------------------------- chat readers
def irc_reader(channel):
    """Anonymous read-only IRC connection to Twitch chat. Counts PRIVMSG lines; reconnects on drop."""
    global msg_count
    channel = channel.lower().lstrip("#")
    while not stopping.is_set():
        try:
            with socket.create_connection((IRC_HOST, IRC_PORT), timeout=30) as s:
                s.settimeout(30)
                s.sendall(b"PASS oauth:SCHMOOPIIE\r\n")
                s.sendall(f"NICK {IRC_NICK}\r\n".encode())
                s.sendall(f"JOIN #{channel}\r\n".encode())
                buf = b""
                log(f"chat: connected anonymously to Twitch #{channel}")
                while not stopping.is_set():
                    try:
                        data = s.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    buf += data
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        if line.startswith(b"PING"):
                            token = line[4:].strip() or b":tmi.twitch.tv"
                            s.sendall(b"PONG " + token + b"\r\n")
                        elif b" PRIVMSG " in line:
                            with count_lock:
                                msg_count += 1
        except OSError as e:
            if not stopping.is_set():
                log(f"chat: Twitch connection notice ({e}), retrying in 3s")
        stopping.wait(3)


def youtube_chat_reader(video_id_or_url):
    """Stream YouTube live chat using pytchat and count incoming messages."""
    global msg_count
    try:
        import pytchat
    except ImportError:
        log("chat: 'pytchat' library is missing. Install with: pip install pytchat")
        return

    while not stopping.is_set():
        try:
            log(f"chat: connecting to YouTube live chat ({video_id_or_url}) ...")
            chat = pytchat.create(video_id=video_id_or_url, interruptable=False)
            log("chat: connected to YouTube live chat successfully!")
            while not stopping.is_set() and chat.is_alive():
                items = list(chat.get().sync_items())
                if items:
                    with count_lock:
                        msg_count += len(items)
                stopping.wait(0.5)
            chat.terminate()
        except Exception as e:
            if not stopping.is_set():
                log(f"chat: YouTube chat notice ({e}), retrying in 4s...")
            stopping.wait(4)


# ---------------------------------------------------------------- workers
def cut_worker(jobs, whisper_q):
    log("STATUS: clip worker started")
    while True:
        job = jobs.get()
        if job is None:
            break
        src, start, dur, out, ready_at = job
        try:
            # Wait until post-spike footage has been written by streamlink.
            if stopping.wait(max(0, ready_at - time.time())):
                log("clip worker: shutdown requested before queued cut")
                continue

            if run_ffmpeg_cut(src, start, dur, out):
                log(f"clip saved: {out}")
                if whisper_q is not None:
                    whisper_q.put(out)
            else:
                log(f"retrying ffmpeg cut with decoding seek for {out}...")
                if run_ffmpeg_cut(src, start, dur, out, accurate_seek=True):
                    log(f"clip saved: {out}")
                    if whisper_q is not None:
                        whisper_q.put(out)
                else:
                    log_error(f"clip generation failed for {out}")
        except Exception as exc:
            log_error(f"clip worker failed for {out}: {exc}")

    if whisper_q is not None:
        whisper_q.put(None)
    log("STATUS: clip worker stopped")


def whisper_worker(q, model_name, outdir):
    log("STATUS: subtitle worker started")
    model_obj = None
    try:
        import whisper
        from whisper.utils import get_writer
        log(f"whisper: loading model '{model_name}' ...")
        model_obj = whisper.load_model(model_name)
        log(f"whisper: model '{model_name}' ready")
    except Exception as e:
        log(f"whisper: direct module load notice ({e}), will use CLI fallback")

    while True:
        clip = q.get()
        if clip is None:
            break
        if not os.path.exists(clip) or os.path.getsize(clip) == 0:
            continue
        log(f"whisper: transcribing {os.path.basename(clip)} ...")
        srt_file = f"{os.path.splitext(clip)[0]}.srt"

        # In-process transcription (avoids reloading PyTorch & weights each time)
        if model_obj is not None:
            try:
                result = model_obj.transcribe(clip, fp16=False)
                writer = get_writer("srt", outdir)
                writer(result, clip)
                log(f"subtitles saved: {srt_file}")
                continue
            except Exception as e:
                log(f"whisper: in-process transcription error ({e}), trying CLI...")

        # CLI fallback via sys.executable -m whisper
        cmd = [
            sys.executable, "-m", "whisper",
            clip, "--model", model_name, "--output_format", "srt",
            "--output_dir", outdir, "--verbose", "False", "--fp16", "False",
        ]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if r.returncode == 0 and os.path.exists(srt_file):
                log(f"subtitles saved: {srt_file}")
            else:
                log_error(f"Whisper failed for {clip}: {(r.stderr or '').strip()[:500]}")
        except OSError as exc:
            log_error(f"Whisper could not start for {clip}: {exc}")
    log("STATUS: subtitle worker stopped")


# ---------------------------------------------------------------- main
def main():
    os.chdir(PROJECT_DIR)
    setup_path()

    ap = argparse.ArgumentParser(
        description="clip-watcher: Record live Twitch or YouTube streams, detect chat spikes, crop 9:16 vertical clips, and generate Whisper subtitles."
    )
    ap.add_argument("channel", help="Twitch channel (e.g. shroud) or YouTube Live URL / video ID")
    ap.add_argument("--min", type=int, default=10, dest="min_msgs",
                    help="minimum messages in a bucket to count as a spike (default 10; quiet chats: 5)")
    ap.add_argument("--factor", type=float, default=2.5, help="spike = factor x median (default 2.5)")
    ap.add_argument("--bucket", type=float, default=10, help="bucket length in seconds (default 10)")
    ap.add_argument("--window", type=float, default=180, help="baseline window in seconds (default 180)")
    ap.add_argument("--pre", type=float, default=20, help="seconds before spike (default 20)")
    ap.add_argument("--post", type=float, default=10, help="seconds after spike (default 10)")
    ap.add_argument("--cooldown", type=float, default=30, help="minimum seconds between clips (default 30)")
    ap.add_argument("--lag", type=float, default=5,
                    help="approx. seconds the recording trails live chat (HLS latency, default 5)")
    ap.add_argument("--quality", default="best", help="stream quality: best, 720p60, 480p, worst, etc. (default: best)")
    ap.add_argument("--whisper-model", default="base", help="Whisper model: tiny, base, small, medium (default: base)")
    ap.add_argument("--no-whisper", action="store_true", help="skip Whisper subtitle generation")
    args = ap.parse_args()

    try:
        retention_settings = load_retention_settings()
    except ConfigurationError as exc:
        log_error(f"invalid retention configuration: {exc}")
        return

    platform, identifier, stream_url = parse_target(args.channel)
    clean_name = re.sub(r"[^\w\-]", "_", identifier)

    rec_dir = PROJECT_DIR / "rec"
    clips_dir = PROJECT_DIR / "clips"
    rec_dir.mkdir(exist_ok=True)
    clips_dir.mkdir(exist_ok=True)
    cleanup_recordings(rec_dir, retention_settings, logger=log)
    rec_path = rec_dir / f"{clean_name}_{datetime.now():%Y%m%d_%H%M%S}.ts"

    # 1) Record stream
    streamlink_bin = resolve_streamlink_command()
    log(f"target detected: [{platform.upper()}] {identifier}")
    log(f"starting streamlink -> {rec_path}")

    sl_cmd = streamlink_bin + [
        "--loglevel", "warning",
        "-o", str(rec_path),
        stream_url, args.quality
    ]
    if platform == "twitch":
        sl_cmd.insert(-3, "--twitch-low-latency")

    try:
        sl = subprocess.Popen(
            sl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        log_error(f"could not start Streamlink: {exc}")
        cleanup_recordings(rec_dir, retention_settings, logger=log)
        return
    threading.Thread(target=relay_process_output, args=(sl, "streamlink"), daemon=True).start()

    # Wait for recording file to be created with data
    deadline = time.time() + 60
    while time.time() < deadline and sl.poll() is None:
        if rec_path.exists() and rec_path.stat().st_size > 0:
            break
        time.sleep(0.5)

    if sl.poll() is not None or not rec_path.exists() or rec_path.stat().st_size == 0:
        log_error(f"could not start recording - is the stream live? Check URL/channel '{args.channel}'.")
        cleanup_recordings(rec_dir, retention_settings, active_recording=rec_path, logger=log)
        return

    t0 = time.time()
    log("recording started")

    # 2) Live Chat Listener
    if platform == "youtube":
        threading.Thread(target=youtube_chat_reader, args=(identifier,), daemon=True).start()
    else:
        threading.Thread(target=irc_reader, args=(identifier,), daemon=True).start()

    # Worker threads
    jobs = queue.Queue()
    wq = None if args.no_whisper else queue.Queue()

    cutter = threading.Thread(target=cut_worker, args=(jobs, wq))
    cutter.start()

    whisperer = None
    if wq is not None:
        whisperer = threading.Thread(target=whisper_worker, args=(wq, args.whisper_model, str(clips_dir)))
        whisperer.start()

    # 3) Buckets + Spike Detection
    global msg_count
    history = deque(maxlen=max(3, int(args.window / args.bucket)))
    min_history = min(6, history.maxlen)  # Warm-up baseline (~1 min)
    last_clip = -1e9
    next_edge = t0 + args.bucket

    try:
        while sl.poll() is None:
            if stopping.wait(max(0, next_edge - time.time())):
                break
            next_edge += args.bucket
            with count_lock:
                c, msg_count = msg_count, 0

            now = time.time()
            med = statistics.median(history) if history else 0
            thr = max(args.min_msgs, args.factor * med)
            ready = len(history) >= min_history
            log(f"chat: {c:4d} msgs / {args.bucket:.0f}s   median={med:.1f}   threshold={thr:.1f}"
                + ("" if ready else "   (warming up)"))

            if ready and c >= thr and (now - last_clip) >= args.cooldown:
                last_clip = now
                pos = (now - t0) + args.lag
                start = max(0.0, pos - args.pre)
                dur = pos + args.post - start
                out = clips_dir / f"{clean_name}_{datetime.now():%H%M%S}_{c}msgs.mp4"
                log(f"SPIKE! {c} msgs (median {med:.1f}) -> clipping {start:.0f}s..{start + dur:.0f}s")
                # Wait until post-spike footage has been written by streamlink
                jobs.put((rec_path, start, dur, out, now + args.post + 3))

            history.append(c)

        if sl.poll() is not None:
            exit_code = sl.returncode
            if exit_code == 0:
                log("stream ended (streamlink exited normally)")
            else:
                log_error(f"streamlink exited unexpectedly with code {exit_code}")
    except KeyboardInterrupt:
        log("stopping...")
    finally:
        stopping.set()
        if sl.poll() is None:
            sl.terminate()
            try:
                sl.wait(10)
            except subprocess.TimeoutExpired:
                sl.kill()
                sl.wait()
        jobs.put(None)
        cutter.join()
        if whisperer:
            whisperer.join()
        cleanup_recordings(rec_dir, retention_settings, active_recording=None, logger=log)
        log("done")


if __name__ == "__main__":
    main()
