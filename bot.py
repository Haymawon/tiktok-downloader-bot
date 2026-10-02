"""
TikTok downloader bot for Telegram.

What it handles:
  * Videos — grabs the mp4 (with audio) and sends it, then rips an
    mp3 out of the same file and sends that as a separate message.
  * Photo / slideshow posts — sends every image as Telegram albums,
    plus the original soundtrack as an audio attachment.

TikTok video downloads go through yt-dlp. Slideshow posts don't work
in yt-dlp at all, so those fall back to TikWM's public API for both
the image list and the music URL.

Requirements:
  * python-telegram-bot >= 20  (async)
  * yt-dlp, httpx, python-dotenv
  * ffmpeg on PATH — needed only for the mp3 extraction step
  * A BOT_TOKEN in .env or the environment

Usage:  python bot.py
"""

import os
import re
import asyncio
import logging
from io import BytesIO
from pathlib import Path

import httpx
import yt_dlp
from dotenv import load_dotenv

from telegram import Update, InputMediaPhoto, constants
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN is missing from .env / environment")

WORKDIR = Path("downloads")
WORKDIR.mkdir(exist_ok=True)

# Telegram refuses bot uploads above ~50 MB. Anything larger gets the
# file deleted and a polite message sent instead, so we don't waste the
# user's time or our bandwidth.
UPLOAD_LIMIT = 49 * 1024 * 1024

# Albums max out at 10 items per group. Longer slideshows get split.
ALBUM_MAX = 10

# Telegram truncates captions past ~1024 chars; trim before it does so
# we control where the cut happens.
CAPTION_MAX = 1000

# Parallel image downloads. Six at a time strikes a decent balance: a
# 15-image slideshow finishes fast, and TikWM's CDN doesn't rate-limit.
IMG_PARALLEL = 6
IMG_TIMEOUT = 20.0
IMG_RETRIES = 2

# When we hand Telegram a URL and ask it to fetch the image itself, it
# either succeeds in a couple seconds or hangs. Cap it so we can bail
# to the slower bytes-download path.
URL_FETCH_TIMEOUT = 8.0

# Progress display bits
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR_LEN = 14


# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tiktok-bot")


# ------------------------------------------------------------------
# Shared HTTP client
# ------------------------------------------------------------------
#
# One client for the whole process. Building a fresh one per request
# throws away connection pool state and TLS sessions, which hurts when
# we're pulling a dozen images in parallel.
#
# The headers are important. TikTok's CDN rejects bare requests — the
# Referer alone decides whether you get a JPEG or a 403. Pretending to
# be desktop Chrome is the combo that's been most stable.

_CLIENT: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(IMG_TIMEOUT, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60.0,
            ),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Referer": "https://www.tiktok.com/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
            http2=False,
        )
    return _CLIENT


# ------------------------------------------------------------------
# URL / text utilities
# ------------------------------------------------------------------

# Matches any TikTok URL, including short-link forms (vm., vt.). We use
# this only to decide whether a message is "for us" — the actual URL is
# handed off to yt-dlp or TikWM which both parse it their own way.
TIKTOK_LINK = re.compile(
    r"(https?://)?(www\.|vm\.|vt\.)?(tiktok\.com|tiktokv\.com)/[^\s]+",
    re.IGNORECASE,
)

# Photo posts have a predictable URL shape. yt-dlp has no idea what to
# do with them, so we spot them early and route accordingly.
PHOTO_LINK = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[\w\.-]+/photo/\d+",
    re.IGNORECASE,
)


def is_tiktok(text: str) -> bool:
    return bool(TIKTOK_LINK.search(text))


def is_photo(url: str) -> bool:
    return bool(PHOTO_LINK.search(url))


def escape(s: str) -> str:
    """Escape the three characters Telegram's HTML parser cares about.

    Ampersand must go first, otherwise we'd double-escape the ones we
    just introduced for < and >.
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def trim_caption(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) > CAPTION_MAX:
        s = s[: CAPTION_MAX - 1].rstrip() + "…"
    return s


def draw_bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 100 * BAR_LEN)
    return "█" * filled + "░" * (BAR_LEN - filled)


# ------------------------------------------------------------------
# Upload progress wrapper
# ------------------------------------------------------------------


class ProgressFile:
    """A file object that counts how many bytes have been read out of it.

    python-telegram-bot reads from whatever object you pass as the
    upload source. Wrapping the file lets us watch the read counter
    climb without touching the network layer at all — the library just
    thinks it's a normal file.
    """

    def __init__(self, path: Path):
        self._fp = open(path, "rb")
        self.sent = 0
        self.size = path.stat().st_size
        self.name = path.name

    def read(self, n=-1):
        chunk = self._fp.read(n)
        self.sent += len(chunk)
        return chunk

    def seek(self, *a):
        return self._fp.seek(*a)

    def tell(self):
        return self._fp.tell()

    def close(self):
        try:
            self._fp.close()
        except Exception:
            pass

    def __getattr__(self, key):
        # Delegate everything else (fileno, readable, etc.) to the real
        # file so the library's file-type detection is happy.
        return getattr(self._fp, key)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


async def _watch_upload(job: dict, fh: ProgressFile):
    """Poll the upload counter so the animator always has fresh numbers."""
    try:
        while True:
            job["done"] = fh.sent
            await asyncio.sleep(0.35)
    except asyncio.CancelledError:
        return


async def _drain(task: asyncio.Task):
    """Await a background task, swallowing whatever it throws.

    Used to clean up the animator and upload-watcher tasks on every exit
    path, including cancellation.
    """
    try:
        await task
    except BaseException:
        pass


# ------------------------------------------------------------------
# Status message animator
# ------------------------------------------------------------------


async def _animate(status_msg, job: dict, stop: asyncio.Event):
    """Redraw the status message until `stop` is set.

    Two display modes, chosen by job["mode"]:

      spinner  — a rotating frame with a label, plus elapsed seconds
                 after the first few seconds (before that it's noise).
      progress — a bar with percentage and MB counters.

    When mode or label changes, we reset the elapsed-time counter so
    "12s" means "12s in the current step", not "12s since the bot
    started". Small thing, but it made the display much less confusing.
    """
    prev = ""
    frame_i = 0
    phase_start = asyncio.get_event_loop().time()
    prev_phase = None

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.8)
            return
        except asyncio.TimeoutError:
            pass

        phase = (job.get("mode"), job.get("label") or job.get("text"))
        if phase != prev_phase:
            phase_start = asyncio.get_event_loop().time()
            prev_phase = phase

        if job.get("mode", "spinner") == "spinner":
            frame = SPINNER[frame_i % len(SPINNER)]
            base = job.get("text", "Working")
            secs = asyncio.get_event_loop().time() - phase_start
            body = f"{frame} <b>{base}</b>"
            if secs > 4:
                body += f"  <i>{secs:.0f}s</i>"
            frame_i += 1
        else:
            done = job.get("done", 0)
            total = job.get("total", 0)
            label = job.get("label", "Working")
            if total > 0:
                pct = done / total * 100
                body = (
                    f"<b>{label}</b>\n"
                    f"<code>{draw_bar(pct)}</code>  {pct:.0f}%\n"
                    f"{done / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB"
                )
            else:
                body = f"<b>{label}</b>\n{done}"

        if body != prev:
            try:
                await status_msg.edit_text(body, parse_mode=constants.ParseMode.HTML)
                prev = body
            except Exception:
                # Telegram rate-limits edits hard. If one fails we just
                # skip this tick and try again on the next one.
                pass


# ------------------------------------------------------------------
# yt-dlp wrapper
# ------------------------------------------------------------------


def _video_opts(template: str) -> dict:
    return {
        "outtmpl": template,
        # Prefer mp4+m4a so the merge is a remux, not a re-encode. The
        # fallback tiers handle TikTok being inconsistent about codecs.
        "format": (
            "bv*[ext=mp4][acodec!=none]+ba[ext=m4a]/"
            "bv*[acodec!=none]+ba[acodec!=none]/"
            "b[ext=mp4][acodec!=none][vcodec!=none]/"
            "b"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 20,
        "format_sort": ["res", "fps", "codec:h264", "size"],
    }


async def grab_video(
    url: str, uid: int, job: dict | None = None
) -> tuple[Path | None, str | None, str]:
    """Download a video URL to disk.

    Returns (path, error_message, title). On success the first element
    is a real file and the second is None; on failure the reverse.
    The caller is responsible for deleting the file afterwards.
    """
    stamp = int(asyncio.get_event_loop().time() * 1000)
    template = str(WORKDIR / f"{uid}_{stamp}.%(ext)s")
    title = ""

    def on_progress(d):
        if job is None:
            return
        try:
            if d.get("status") == "downloading":
                job["done"] = d.get("downloaded_bytes", 0)
                job["total"] = (
                    d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                )
            elif d.get("status") == "finished":
                # Some CDNs never report a total until the last byte.
                # Backfill it so the bar can finish at 100%.
                if job.get("total", 0) == 0:
                    job["total"] = job.get("done", 0)
                job["done"] = job.get("total", 0)
        except Exception:
            pass

    opts = _video_opts(template)
    opts["progress_hooks"] = [on_progress]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            if not info:
                return None, "Couldn't read that video.", ""

            # This log has saved me more times than I can count. When a
            # video comes out silent, 9 times in 10 the acodec here
            # says "none" — which means ffmpeg isn't on PATH.
            vcodec = info.get("vcodec") or "?"
            acodec = info.get("acodec") or "?"
            logger.info(
                f"format picked: id={info.get('format_id', '?')} "
                f"v={vcodec} a={acodec}"
            )
            if acodec in (None, "none"):
                logger.warning(
                    "downloaded stream has no audio — is ffmpeg installed?"
                )

            title = (info.get("title") or info.get("description") or "").strip()

            path = Path(ydl.prepare_filename(info))

            # prepare_filename can miss when yt-dlp remuxed the output
            # into a different container. Fall back to whatever landed
            # with our stamp in the name.
            if not path.exists():
                for candidate in WORKDIR.glob(f"{uid}_{stamp}*"):
                    if candidate.is_file():
                        path = candidate
                        break

            if not path.exists():
                return None, "Download finished but the file vanished.", title

            size = path.stat().st_size
            if size > UPLOAD_LIMIT:
                path.unlink(missing_ok=True)
                return (
                    None,
                    f"Video is {size / 1024 / 1024:.1f} MB — over Telegram's "
                    f"~50 MB bot upload cap.",
                    title,
                )
            return path, None, title

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp failed: {e}")
        return None, str(e)[:200], title
    except Exception as e:
        logger.exception("unexpected error during video download")
        return None, f"{type(e).__name__}: {e!r}"[:200], title


# ------------------------------------------------------------------
# mp3 extraction (ffmpeg)
# ------------------------------------------------------------------


async def rip_audio(src: Path, uid: int) -> Path | None:
    """Run ffmpeg to pull an mp3 out of a video file.

    Returns the mp3 path on success, or None if ffmpeg is missing / the
    extraction fails. The caller owns the resulting file.
    """
    stamp = int(asyncio.get_event_loop().time() * 1000)
    dest = WORKDIR / f"{uid}_{stamp}_audio.mp3"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",                      # overwrite if it somehow exists
        "-i", str(src),
        "-vn",                     # drop the video stream
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        str(dest),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                f"ffmpeg mp3 extract failed (rc={proc.returncode}): "
                f"{err.decode(errors='ignore')[:200]}"
            )
            dest.unlink(missing_ok=True)
            return None

        if not dest.exists() or dest.stat().st_size == 0:
            logger.warning("ffmpeg produced an empty mp3")
            return None

        return dest

    except FileNotFoundError:
        logger.error("ffmpeg not on PATH — skipping mp3")
        return None
    except Exception as e:
        logger.exception(f"mp3 extract blew up: {e!r}")
        dest.unlink(missing_ok=True)
        return None


# ------------------------------------------------------------------
# TikWM probe (for slideshow posts)
# ------------------------------------------------------------------


async def probe_slideshow(
    url: str,
) -> tuple[list[str], str | None, str, str | None]:
    """Ask TikWM about a photo post.

    Returns (image_urls, music_url, title, error). yt-dlp can't touch
    slideshows at all, so this is the only path for those. TikWM's API
    is undocumented but has been stable for years.
    """
    try:
        r = await client().get(
            "https://www.tikwm.com/api/", params={"url": url}, timeout=25.0
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.error(f"tikwm call failed: {type(e).__name__}: {e!r}")
        return [], None, "", f"TikTok API unreachable ({type(e).__name__})."

    if payload.get("code") != 0:
        return [], None, "", f"TikTok API: {payload.get('msg', 'unknown error')}"

    data = payload.get("data") or {}
    images = data.get("images") or []
    music = data.get("music") or (data.get("music_info") or {}).get("play")
    title = (data.get("title") or "").strip()

    if not images and not music:
        return [], None, title, "This post has no photos or audio."

    logger.info(
        f"tikwm: {len(images)} image(s), music={'yes' if music else 'no'}"
    )
    return list(images), music, title, None


# ------------------------------------------------------------------
# Byte fetching
# ------------------------------------------------------------------


async def _get_bytes(
    url: str, tag: str, retries: int = IMG_RETRIES
) -> tuple[bytes | None, str | None]:
    """Fetch a URL and return its body as bytes.

    Retries a couple of times on transient failures — TikTok's CDN
    occasionally hiccups on first request and succeeds on the second.
    """
    c = client()
    last = "unknown"

    for attempt in range(retries + 1):
        try:
            r = await c.get(url)
            r.raise_for_status()
            if not r.content:
                raise ValueError("empty body")
            return r.content, None
        except Exception as e:
            last = (str(e) or type(e).__name__)[:120]
            logger.warning(f"{tag} attempt {attempt + 1}: {type(e).__name__}")
            if attempt < retries:
                # Small linear backoff. Keeps us from hammering a CDN
                # that's already struggling.
                await asyncio.sleep(0.5 * (attempt + 1))

    return None, last


async def fetch_all_images(
    urls: list[str], job: dict | None = None
) -> list[bytes]:
    """Download every image in parallel and return them in original order."""
    gate = asyncio.Semaphore(IMG_PARALLEL)
    total = len(urls)
    done_count = {"n": 0}

    if job is not None:
        job["mode"] = "progress"
        job["label"] = "⬇️ Downloading images"
        job["done"] = 0
        job["total"] = total

    async def one(idx: int, u: str):
        async with gate:
            content, err = await _get_bytes(u, f"img[{idx + 1}/{total}]")
            done_count["n"] += 1
            if job is not None:
                job["done"] = done_count["n"]
            return idx, content, err

    results = await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)))

    out: list[bytes] = []
    for i, content, err in sorted(results, key=lambda r: r[0]):
        if content is not None:
            out.append(content)
        else:
            logger.warning(f"img[{i + 1}] gave up: {err}")
    return out


# ------------------------------------------------------------------
# UI strings
# ------------------------------------------------------------------


def welcome() -> str:
    return (
        "🎬 <b>TikTok Downloader Bot</b>\n\n"
        "Paste any TikTok link. I'll grab:\n"
        "• Videos, with sound\n"
        "• The audio as a separate mp3\n"
        "• Photo slideshows, with their original soundtrack\n\n"
        "That's it. Just send a link."
    )


def failure(msg: str) -> str:
    return f"❌ <b>Couldn't do that</b>\n\n<code>{escape(msg)}</code>"


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        welcome(),
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    uid = msg.from_user.id

    if not is_tiktok(text):
        await msg.reply_text(
            "🤔 That's not a TikTok link.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    hit = TIKTOK_LINK.search(text)
    url = hit.group(0)
    if not url.startswith("http"):
        url = "https://" + url

    # A placeholder status message that the animator will take over.
    status = await msg.reply_text(
        "⠋ <b>Starting…</b>", parse_mode=constants.ParseMode.HTML
    )
    await _route(msg, status, url, uid)


async def _route(msg, status, url: str, uid: int):
    """Figure out what kind of post this is, fetch it, and hand off to
    the right sender. Everything runs under a live status message that
    is always cleaned up, no matter how we exit."""
    job = {"mode": "spinner", "text": "Fetching post info", "done": 0, "total": 0}
    stop = asyncio.Event()
    animator = asyncio.create_task(_animate(status, job, stop))

    local_file: Path | None = None
    try:
        slideshow = is_photo(url)
        images: list[str] = []
        music: str | None = None
        title = ""
        err: str | None = None

        if slideshow:
            images, music, title, err = await probe_slideshow(url)
        else:
            job["mode"] = "progress"
            job["label"] = "⬇️ Downloading video"
            job["done"] = 0
            job["total"] = 0
            local_file, err, title = await grab_video(url, uid, job)

            # yt-dlp occasionally chokes on brand-new post types. When
            # it says "unsupported", it's usually a slideshow that
            # slipped past our URL check. TikWM knows about those.
            if err and "unsupported" in err.lower():
                logger.info("yt-dlp unsupported — trying tikwm instead")
                job["mode"] = "spinner"
                job["text"] = "Fetching post info"
                images, music, alt_title, photo_err = await probe_slideshow(url)
                if images or music:
                    err = None
                    slideshow = True
                    if alt_title:
                        title = alt_title
                else:
                    err = photo_err or err

        has_anything = bool(images or music or local_file)
        if err and not has_anything:
            stop.set()
            await _drain(animator)
            await status.edit_text(
                failure(err), parse_mode=constants.ParseMode.HTML
            )
            return

        try:
            if slideshow:
                await send_slideshow(msg, images, music, title, status, job)
            elif local_file:
                await send_clip(msg, local_file, title, status, job, uid)
            else:
                stop.set()
                await _drain(animator)
                await status.edit_text(
                    failure("Nothing to send."),
                    parse_mode=constants.ParseMode.HTML,
                )
                return
        finally:
            if local_file:
                try:
                    local_file.unlink(missing_ok=True)
                except Exception:
                    pass
    finally:
        stop.set()
        await _drain(animator)


# ------------------------------------------------------------------
# Senders
# ------------------------------------------------------------------


async def send_clip(
    msg,
    path: Path,
    title: str,
    status,
    job: dict,
    uid: int,
):
    """Upload the video, then extract and send an mp3 of its audio."""
    caption = escape(trim_caption(title)) if title else "✅ <b>Here you go.</b>"

    fh = ProgressFile(path)
    job["mode"] = "progress"
    job["label"] = "📤 Uploading video"
    job["done"] = 0
    job["total"] = fh.size

    watcher = asyncio.create_task(_watch_upload(job, fh))
    try:
        await msg.reply_video(
            video=fh,
            caption=caption,
            parse_mode=constants.ParseMode.HTML,
            supports_streaming=True,
            # Big uploads over slow links take a while. Give them room
            # instead of giving up halfway.
            read_timeout=180,
            write_timeout=180,
            connect_timeout=60,
        )
    finally:
        watcher.cancel()
        await _drain(watcher)
        fh.close()

    # Now the audio track. Sent as a separate message so users can
    # forward it on its own.
    job["mode"] = "spinner"
    job["text"] = "🎵 Extracting audio"
    mp3 = await rip_audio(path, uid)

    if mp3 is not None:
        job["mode"] = "spinner"
        job["text"] = "🎵 Sending audio"
        try:
            with open(mp3, "rb") as f:
                await msg.reply_audio(
                    audio=f,
                    caption="🎵 <b>Audio track.</b>",
                    parse_mode=constants.ParseMode.HTML,
                    read_timeout=180,
                    write_timeout=180,
                )
        except TelegramError as e:
            logger.warning(f"sending mp3 failed: {e}")
        finally:
            try:
                mp3.unlink(missing_ok=True)
            except Exception:
                pass
    else:
        logger.warning("no mp3 this time — video sent without an audio file")

    await status.delete()


async def send_slideshow(
    msg,
    image_urls: list[str],
    music_url: str | None,
    title: str,
    status,
    job: dict,
):
    """Send a slideshow.

    Tries URL-passthrough first (fast — Telegram fetches the images
    itself, no bytes pass through us). Falls back to downloading and
    re-uploading when Telegram can't reach TikTok's CDN, which happens
    from time to time.
    """
    cap = escape(trim_caption(title)) if title else None

    sent_via_url = False
    if image_urls:
        job["mode"] = "spinner"
        job["text"] = "📤 Sending images"
        try:
            await _photos_from_urls(msg, image_urls, cap)
            sent_via_url = True
        except (TelegramError, asyncio.TimeoutError) as e:
            logger.warning(
                f"URL-passthrough send failed ({type(e).__name__}) — "
                f"falling back to bytes"
            )
            sent_via_url = False

    if image_urls and not sent_via_url:
        blobs = await fetch_all_images(image_urls, job)
        if blobs:
            job["mode"] = "spinner"
            job["text"] = "📤 Sending images"
            await _photos_from_bytes(msg, blobs, cap)

    if music_url:
        job["mode"] = "spinner"
        job["text"] = "🎵 Sending soundtrack"
        try:
            await msg.reply_audio(
                audio=music_url,
                caption="🎵 <b>Original soundtrack.</b>",
                parse_mode=constants.ParseMode.HTML,
                read_timeout=60,
                write_timeout=60,
            )
        except TelegramError:
            # Same story as the images — pull it down and re-upload.
            logger.warning("URL music send failed — fetching bytes")
            blob, _ = await _get_bytes(music_url, "music")
            if blob:
                ext = "mp3"
                lower = music_url.lower()
                if ".m4a" in lower:
                    ext = "m4a"
                elif ".wav" in lower:
                    ext = "wav"
                buf = BytesIO(blob)
                buf.name = f"soundtrack.{ext}"
                await msg.reply_audio(
                    audio=buf,
                    caption="🎵 <b>Original soundtrack.</b>",
                    parse_mode=constants.ParseMode.HTML,
                )

    await status.delete()


async def _photos_from_urls(msg, urls: list[str], cap: str | None):
    """Send images by handing TikTok URLs straight to Telegram."""
    batches = [urls[i: i + ALBUM_MAX] for i in range(0, len(urls), ALBUM_MAX)]

    for idx, batch in enumerate(batches):
        first_batch = idx == 0

        if len(batch) == 1:
            await msg.reply_photo(
                photo=batch[0],
                caption=cap if first_batch else None,
                parse_mode=(
                    constants.ParseMode.HTML if first_batch and cap else None
                ),
                read_timeout=URL_FETCH_TIMEOUT,
                write_timeout=URL_FETCH_TIMEOUT,
            )
        else:
            album: list[InputMediaPhoto] = []
            for i, u in enumerate(batch):
                if first_batch and i == 0 and cap:
                    album.append(
                        InputMediaPhoto(
                            media=u,
                            caption=cap,
                            parse_mode=constants.ParseMode.HTML,
                        )
                    )
                else:
                    album.append(InputMediaPhoto(media=u))
            await msg.reply_media_group(
                media=album,
                read_timeout=URL_FETCH_TIMEOUT,
                write_timeout=URL_FETCH_TIMEOUT,
            )


async def _photos_from_bytes(msg, blobs: list[bytes], cap: str | None):
    """Send images we downloaded ourselves. Used when URL-passthrough
    failed, or when Telegram can't reach the source."""
    batches = [blobs[i: i + ALBUM_MAX] for i in range(0, len(blobs), ALBUM_MAX)]

    for idx, batch in enumerate(batches):
        first_batch = idx == 0

        if len(batch) == 1:
            buf = BytesIO(batch[0])
            buf.name = "photo.jpg"
            await msg.reply_photo(
                photo=buf,
                caption=cap if first_batch else None,
                parse_mode=(
                    constants.ParseMode.HTML if first_batch and cap else None
                ),
                read_timeout=180,
                write_timeout=180,
            )
        else:
            album: list[InputMediaPhoto] = []
            for i, b in enumerate(batch):
                buf = BytesIO(b)
                buf.name = f"photo_{i:02d}.jpg"
                if first_batch and i == 0 and cap:
                    album.append(
                        InputMediaPhoto(
                            media=buf,
                            caption=cap,
                            parse_mode=constants.ParseMode.HTML,
                        )
                    )
                else:
                    album.append(InputMediaPhoto(media=buf))
            await msg.reply_media_group(
                media=album, read_timeout=180, write_timeout=180
            )


# ------------------------------------------------------------------
# Error handling / lifecycle
# ------------------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("unhandled exception during update", exc_info=context.error)


async def _shutdown(app: Application):
    """Close the shared HTTP client before the loop stops."""
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        # Bumped well past the defaults. Telegram's API is slow from
        # some regions and the stock timeouts cause spurious failures.
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(40)
        .get_updates_connect_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .post_shutdown(_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    logger.info("bot up")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
