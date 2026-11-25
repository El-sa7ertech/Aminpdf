import os, glob, time, shutil, tempfile, threading, traceback
from itertools import islice
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import telebot, yt_dlp

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_ID = int(os.environ["MY_ID"])
DEBUG = os.environ.get("DEBUG") == "1"
MAX_VIDEOS = 20
MAX_TARGETS = 10
MAX_SIZE = 49 * 1024**2
ALLOWED_DOMAINS = ("facebook.com", "fb.watch", "youtube.com", "youtu.be")
busy = threading.Lock()


# ---- خادم صغير لـ Render (GET و HEAD) ----
class Ping(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        if body:
            self.wfile.write(b"ok")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(False)

    def log_message(self, *a):
        pass


def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


threading.Thread(target=run_server, daemon=True).start()


# ---- الكوكيز ----
SRC = os.environ.get("COOKIES_SRC", "/etc/secrets/cookies.txt")
COOKIES = os.path.join(tempfile.gettempdir(), "cookies.txt")
try:
    shutil.copyfile(SRC, COOKIES)
    print("cookies loaded", flush=True)
except Exception:
    COOKIES = None
    print("running without cookies", flush=True)


# ---- ffmpeg ----
def find_ffmpeg():
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
print("ffmpeg:", FFMPEG, flush=True)

bot = telebot.TeleBot(TOKEN)
os.makedirs("dl", exist_ok=True)

LIST_OPTS = {
    "extract_flat": "in_playlist",
    "playlistend": MAX_VIDEOS,
    "quiet": True,
    "cookiefile": COOKIES,
    "socket_timeout": 30,
}


def make_opts(base):
    opts = {
        "outtmpl": base + ".%(ext)s",
        "cookiefile": COOKIES,
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }]
    else:
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    return opts


def domain_ok(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def to_urls(t):
    if t.startswith("http"):
        return [t] if domain_ok(t) else []
    if t.replace(".", "").replace("-", "").replace("_", "").isalnum():
        # اسم صفحة: جرّب تبويب الريلز أولًا ثم الفيديوهات
        return [f"https://www.facebook.com/{t}/reels",
                f"https://www.facebook.com/{t}/videos"]
    return []


def human_size(n):
    return f"{n / 1024**2:.2f} MB"


def fmt_duration(sec):
    if not sec:
        return "؟"
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def list_entries(url):
    with yt_dlp.YoutubeDL(LIST_OPTS) as ydl:
        info = ydl.extract_info(url, download=False) or {}
        entries = info.get("entries")
        if entries is None:  # مقطع واحد
            return [{"url": info.get("webpage_url") or url,
                     "title": info.get("title")}]
        out = []
        for e in islice(entries, MAX_VIDEOS):
            if not e:
                continue
            u = e.get("url") or e.get("webpage_url")
            if u:
                out.append({"url": u, "title": e.get("title")})
        return out


def cleanup(base):
    for p in glob.glob(base + ".*"):
        try:
            os.remove(p)
        except OSError:
            pass


def send_file(chat_id, path, title):
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        if ext in (".mp3", ".m4a"):
            bot.send_audio(chat_id, f, title=title, timeout=120)
        else:
            bot.send_document(chat_id, f, timeout=120)


def download_one(m, e, idx):
    base = f"dl/{int(time.time())}_{idx}"
    name = e.get("title") or "مقطع"
    try:
        with yt_dlp.YoutubeDL(make_opts(base)) as ydl:
            info = ydl.extract_info(e["url"], download=True) or {}
        files = [p for p in glob.glob(base + ".*")
                 if not p.endswith((".part", ".ytdl"))]
        files.sort(key=lambda p: p.endswith(".mp3"), reverse=True)
        if not files:
            bot.send_message(m.chat.id, f"تعذر تحميل: {name}")
            return
        path = files[0]
        size = os.path.getsize(path)
        title = info.get("title") or name
        desc = (info.get("description") or "").strip()
        details = (f"📦 الحجم: {human_size(size)}\n"
                   f"⏱ المدة: {fmt_duration(info.get('duration'))}\n"
                   f"🔗 {info.get('webpage_url') or e['url']}")
        if desc and desc != title:
            details += f"\n\n📝 النص المكتوب:\n{desc}"
        elif title:
            details += f"\n\n📝 {title}"

        if size > MAX_SIZE:
            bot.send_message(m.chat.id,
                             f"المقطع كبير جدًا ولم يُرسل:\n{details}"[:4000])
            return
        send_file(m.chat.id, path, title)
        bot.send_message(m.chat.id, details[:4000])
        time.sleep(2)
    except Exception as ex:
        traceback.print_exc()
        msg = f"فشل مقطع: {name}"
        if DEBUG:
            msg += "\n" + str(ex)[:300]
        bot.send_message(m.chat.id, msg)
    finally:
        cleanup(base)


def process(m, targets):
    bot.reply_to(m, "جاري جلب قائمة المقاطع...")
    entries = []
    for t in targets:
        urls = to_urls(t)
        if not urls:
            bot.send_message(m.chat.id, "مدخل غير صالح أو دومين غير مدعوم")
            continue
        found, last_err = [], None
        for url in urls:
            try:
                found = list_entries(url)
                if found:
                    break
            except Exception as ex:
                traceback.print_exc()
                last_err = ex
        if found:
            entries += found
        else:
            msg = "فشل جلب القائمة. إن كان الرابط لصفحة فيسبوك فأرسل روابط المقاطع نفسها"
            if DEBUG and last_err:
                msg += "\n" + str(last_err)[:300]
            bot.send_message(m.chat.id, msg)

    # إزالة التكرار مع الحفاظ على الترتيب
    seen, uniq = set(), []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            uniq.append(e)
    entries = uniq

    entries = entries[:MAX_VIDEOS]
    if not entries:
        bot.send_message(m.chat.id, "لم أجد مقاطع")
        return
    bot.send_message(m.chat.id, f"سأرسل {len(entries)} مقطع")

    for i, e in enumerate(entries):
        download_one(m, e, i)

    bot.send_message(m.chat.id, "انتهيت ✅")


@bot.message_handler(func=lambda m: True)
def handle(m):
    if m.from_user.id != ALLOWED_ID:
        return
    text = (m.text or "").strip()
    if not text or len(text) > 2000:
        return
    targets = text.split()[:MAX_TARGETS]

    if not busy.acquire(blocking=False):
        bot.reply_to(m, "أنا مشغول بمهمة سابقة، انتظر انتهاءها")
        return
    try:
        process(m, targets)
    finally:
        busy.release()


bot.infinity_polling(skip_pending=True)

