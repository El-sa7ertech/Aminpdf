import os, time, shutil, tempfile, threading, traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import telebot, yt_dlp

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_ID = int(os.environ["MY_ID"])
MAX_VIDEOS = 20
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

bot = telebot.TeleBot(TOKEN)
os.makedirs("dl", exist_ok=True)

OPTS = {
    "format": "bestaudio/best",
    "outtmpl": "dl/%(id)s.%(ext)s",
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "64",
    }],
    "cookiefile": COOKIES,
    "quiet": True,
    "ignoreerrors": True,
}
LIST_OPTS = {"extract_flat": True, "quiet": True, "cookiefile": COOKIES}


def domain_ok(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def process(m, url):
    bot.reply_to(m, "جاري جلب قائمة المقاطع...")
    try:
        with yt_dlp.YoutubeDL(LIST_OPTS) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception:
        traceback.print_exc()
        bot.send_message(m.chat.id, "فشل جلب القائمة")
        return

    entries = info.get("entries")
    if entries is None and info.get("id"):  # رابط مقطع واحد
        entries = [{
            "id": info["id"],
            "url": info.get("webpage_url") or url,
            "title": info.get("title"),
        }]
    entries = [e for e in (entries or []) if e and e.get("id") and e.get("url")]
    entries = entries[:MAX_VIDEOS]

    if not entries:
        bot.send_message(m.chat.id, "لم أجد مقاطع")
        return
    bot.send_message(m.chat.id, f"سأرسل {len(entries)} مقطع")

    for e in entries:
        path = f"dl/{e['id']}.mp3"
        name = e.get("title") or e["id"]
        try:
            with yt_dlp.YoutubeDL(OPTS) as ydl:
                ydl.download([e["url"]])
            if not os.path.exists(path):
                bot.send_message(m.chat.id, f"تعذر تحميل: {name}")
                continue
            if os.path.getsize(path) > MAX_SIZE:
                bot.send_message(m.chat.id, f"المقطع كبير جدًا: {name}")
                continue
            with open(path, "rb") as f:
                bot.send_audio(m.chat.id, f, title=e.get("title"), timeout=120)
            time.sleep(2)
        except Exception:
            traceback.print_exc()
            bot.send_message(m.chat.id, f"فشل مقطع: {name}")
        finally:
            if os.path.exists(path):
                os.remove(path)

    bot.send_message(m.chat.id, "انتهيت ✅")


@bot.message_handler(func=lambda m: True)
def handle(m):
    if m.from_user.id != ALLOWED_ID:
        return
    page = (m.text or "").strip()
    if not page or len(page) > 200:
        return

    if page.startswith("http"):
        if not domain_ok(page):
            bot.reply_to(m, "رابط غير مدعوم")
            return
        url = page
    else:
        if not page.replace(".", "").replace("-", "").replace("_", "").isalnum():
            bot.reply_to(m, "اسم غير صالح")
            return
        url = f"https://www.facebook.com/{page}/videos"

    if not busy.acquire(blocking=False):
        bot.reply_to(m, "أنا مشغول بمهمة سابقة، انتظر انتهاءها")
        return
    try:
        process(m, url)
    finally:
        busy.release()


bot.infinity_polling()
