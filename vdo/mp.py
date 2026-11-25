import os, glob, time, json, shutil, tempfile, threading, traceback
from itertools import islice
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import telebot, yt_dlp

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_ID = int(os.environ["MY_ID"])
DEBUG = os.environ.get("DEBUG") == "1"
MAX_VIDEOS = 20          # عدد المقاطع في كل دفعة
MAX_LIST = 1000          # أقصى عدد مقاطع يتم عدّها في الصفحة
MAX_TARGETS = 10
MAX_SIZE = 49 * 1024**2
PROGRESS_FILE = "progress.json"
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
    "playlistend": MAX_LIST,
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


# ---- حفظ مكان التوقف لكل صفحة ----
def load_progress():
    try:
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_progress(data):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        traceback.print_exc()


def fmt_size(n):
    return f"{n / 1024**2:.1f} MB"


def domain_ok(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def to_url(t):
    if t.startswith("http"):
        return t if domain_ok(t) else None
    if t.replace(".", "").replace("-", "").replace("_", "").isalnum():
        return f"https://www.facebook.com/{t}/videos"
    return None


def list_entries(url):
    """يرجع (قائمة المقاطع, هل هو صفحة/قائمة تشغيل)"""
    with yt_dlp.YoutubeDL(LIST_OPTS) as ydl:
        info = ydl.extract_info(url, download=False) or {}
        entries = info.get("entries")
        if entries is None:  # مقطع واحد
            return [{"url": info.get("webpage_url") or url,
                     "title": info.get("title")}], False
        out = []
        for e in islice(entries, MAX_LIST):
            if not e:
                continue
            u = e.get("url") or e.get("webpage_url")
            if u:
                out.append({"url": u, "title": e.get("title")})
        return out, True


def cleanup(base):
    for p in glob.glob(base + ".*"):
        try:
            os.remove(p)
        except OSError:
            pass


def send_file(chat_id, path, title, caption):
    ext = os.path.splitext(path)[1].lower()
    caption = caption[:1024]
    with open(path, "rb") as f:
        if ext in (".mp3", ".m4a", ".opus", ".ogg"):
            bot.send_audio(chat_id, f, title=title, caption=caption, timeout=120)
        else:
            bot.send_document(chat_id, f, caption=caption, timeout=120)


def send_long(chat_id, text):
    for i in range(0, len(text), 4000):
        bot.send_message(chat_id, text[i:i + 4000])


def download_one(m, e, idx, label):
    base = f"dl/{int(time.time())}_{idx}"
    name = e.get("title") or "مقطع"
    try:
        with yt_dlp.YoutubeDL(make_opts(base)) as ydl:
            info = ydl.extract_info(e["url"], download=True) or {}
        files = [p for p in glob.glob(base + ".*")
                 if not p.endswith((".part", ".ytdl"))]
        files.sort(key=lambda p: p.endswith(".mp3"), reverse=True)
        if not files:
            bot.send_message(m.chat.id, f"[{label}] تعذر تحميل: {name}")
            return
        path = files[0]
        size = os.path.getsize(path)
        if size > MAX_SIZE:
            bot.send_message(
                m.chat.id,
                f"[{label}] المقطع كبير جدًا ({fmt_size(size)}): {name}")
            return

        title = info.get("title") or name
        desc = (info.get("description") or "").strip()
        caption = f"📦 {fmt_size(size)} | [{label}]\n{title}"
        send_file(m.chat.id, path, title, caption)

        # الكتابة المرفقة بالفيديو (إن وُجدت وكانت مختلفة عن العنوان)
        if desc and desc != title.strip():
            send_long(m.chat.id, "📝 " + desc)
        time.sleep(2)
    except Exception as ex:
        traceback.print_exc()
        msg = f"[{label}] فشل مقطع: {name}"
        if DEBUG:
            msg += "\n" + str(ex)[:300]
        bot.send_message(m.chat.id, msg)
    finally:
        cleanup(base)


def process(m, targets):
    bot.reply_to(m, "جاري جلب قائمة المقاطع...")
    progress = load_progress()
    batch = []
    quota = MAX_VIDEOS

    for t in targets:
        if quota <= 0:
            bot.send_message(m.chat.id, f"تجاوزت {MAX_VIDEOS} مقطع، تجاهلت باقي المدخلات")
            break
        url = to_url(t)
        if not url:
            bot.send_message(m.chat.id, "مدخل غير صالح أو دومين غير مدعوم")
            continue
        try:
            entries, is_list = list_entries(url)
        except Exception as ex:
            traceback.print_exc()
            msg = "فشل جلب القائمة. إن كان الرابط لصفحة فيسبوك فأرسل روابط المقاطع نفسها"
            if DEBUG:
                msg += "\n" + str(ex)[:300]
            bot.send_message(m.chat.id, msg)
            continue

        if not is_list:  # مقطع واحد
            batch += [(e, "1/1", None, 0) for e in entries[:1]]
            quota -= 1
            continue

        total = len(entries)
        if total == 0:
            bot.send_message(m.chat.id, "لم أجد مقاطع في هذه الصفحة")
            continue
        plus = "+" if total >= MAX_LIST else ""
        start = progress.get(url, 0)
        if start >= total:
            progress.pop(url, None)
            save_progress(progress)
            bot.send_message(
                m.chat.id,
                f"📊 عدد المقاطع في الصفحة: {total}{plus}\n"
                f"✅ أرسلت كل المقاطع سابقًا. أرسل الرابط مرة أخرى لتبدأ من الأول.")
            continue

        chunk = entries[start:start + quota]
        end = start + len(chunk)
        bot.send_message(
            m.chat.id,
            f"📊 عدد المقاطع في الصفحة: {total}{plus}\n"
            f"سأرسل من {start + 1} إلى {end}"
            + (f"\nالمتبقي بعدها: {total - end}" if total > end else ""))
        batch += [(e, f"{start + i + 1}/{total}", url, start + i + 1)
                  for i, e in enumerate(chunk)]
        quota -= len(chunk)

    if not batch:
        bot.send_message(m.chat.id, "لم أجد مقاطع")
        return

    for i, (e, label, key, pos) in enumerate(batch):
        download_one(m, e, i, label)
        if key:
            progress[key] = pos
            save_progress(progress)

    bot.send_message(m.chat.id, "انتهيت ✅")


@bot.message_handler(func=lambda m: True)
def handle(m):
    if m.from_user.id != ALLOWED_ID:
        return
    text = (m.text or "").strip()
    if not text or len(text) > 2000:
        return

    # /reset => يبدأ من الأول لكل الصفحات
    if text.split()[0].lower().startswith("/reset"):
        if busy.locked():
            bot.reply_to(m, "أنا مشغول، أعد /reset بعد الانتهاء")
            return
        save_progress({})
        bot.reply_to(m, "تم تصفير التقدم، ستبدأ كل صفحة من أول مقطع")
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

