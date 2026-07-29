"""
بوت تلجرام: يستقبل ملف PDF (أو رابط PDF) -> يستخرج كل الصور اللي جواه -> ينشرها على صفحة فيسبوك.

الملف ده بيقسم الـ PDF لمجموعات من الصفحات (افتراضيًا 20 صفحة)، وكل مجموعة
بتتنشر في بوست منفصل يجمع صور صفحاتها. يعني PDF من 100 صفحة هيتقسم على
5 بوستات (20 صفحة لكل بوست).

البوت بيقبل نوعين من المدخلات:
    1. ملف PDF مرفوع مباشرة كـ Document.
    2. رسالة نصية فيها رابط (بينتهي بـ .pdf أو أي رابط عمومًا، وهيتأكد
       البوت من نوع المحتوى الفعلي بعد التحميل بدل ما يعتمد على الامتداد بس).

نسخة Webhook — مخصصة للتشغيل على Render كـ Web Service (مش Background Worker).

المكتبات المطلوبة (requirements.txt):
    python-telegram-bot[webhooks]
    PyMuPDF
    requests

الإعدادات المطلوبة (Environment Variables في Render):
    TELEGRAM_BOT_TOKEN   -> توكن البوت من BotFather
    FB_PAGE_ID           -> ID بتاع صفحة الفيسبوك
    FB_PAGE_ACCESS_TOKEN -> Page Access Token (long-lived) من Graph API
    WEBHOOK_URL          -> رابط السيرفس على ريندر، مثال:
                            https://your-service-name.onrender.com
    ALLOWED_USER_IDS     -> (اختياري) أرقام يوزرات تليجرام المسموح لهم، مفصولة بفاصلة
                            مثال: 123456789,987654321
                            لو سبتها فاضية، أي حد هيقدر يستخدم البوت وينشر على صفحتك!

طريقة الحصول على FB_PAGE_ACCESS_TOKEN:
    1. اعمل Facebook App من developers.facebook.com
    2. خد User Access Token من Graph API Explorer بصلاحية pages_manage_posts + pages_read_engagement
    3. بدّله بـ Long-Lived Token
    4. من /me/accounts هتجيبله Page Access Token بتاع صفحتك (ده اللي بيتحط هنا)

إعداد Render:
    - Service type: Web Service (مش Background Worker)
    - Build Command:  pip install -r requirements.txt
    - Start Command:  python bot-1.py
    - لازم تحط WEBHOOK_URL = رابط السيرفس نفسه بعد ما ينعمل Deploy أول مرة
"""

import os
import io
import re
import time
import json
import asyncio
import logging
import tempfile
import requests
import fitz  # PyMuPDF

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# بيكتشف أي رابط http/https جوه الرسالة النصية
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- الإعدادات ----
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TELEGRAM_TOKEN_HERE")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "PUT_YOUR_PAGE_ID_HERE")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "PUT_YOUR_PAGE_ACCESS_TOKEN_HERE")

# ريندر بيدي البورت في متغير البيئة PORT تلقائيًا
PORT = int(os.environ.get("PORT", "10000"))

# لازم يكون رابط السيرفس بتاعك على ريندر (من غير / في الآخر)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")

# مسار سري بسيط للـ webhook، بيتبني من التوكن نفسه عشان محدش يقدر يخمنه
WEBHOOK_PATH = TELEGRAM_BOT_TOKEN

# قائمة اليوزرات المسموح لهم يستخدموا البوت (لو فاضية = الكل مسموح، مش موصى بيه)
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

MIN_IMAGE_BYTES = 3000  # علشان نتجاهل صور صغيرة جدًا (أيقونات/خطوط مدمجة)

# حد أقصى لحجم أي PDF بيتحمل من رابط (بالبايت)، عشان مايبقاش فيه استغلال
# برابط بيرجّع ملف ضخم يفجر الرام. القيمة الافتراضية هنا 300 ميجا
# (البوت لاستخدام شخصي، فمفيش داعي لحد صغير زي حالة البوت العام).
MAX_PDF_DOWNLOAD_BYTES = int(os.environ.get("MAX_PDF_DOWNLOAD_BYTES", str(300 * 1024 * 1024)))

# عدد صفحات الـ PDF اللي بتتجمع صورها في بوست واحد.
# مثال: PDF من 100 صفحة مع PAGES_PER_POST = 20 هيتقسم على 5 بوستات.
PAGES_PER_POST = int(os.environ.get("PAGES_PER_POST", "20"))


def iter_page_chunks(doc: "fitz.Document", pages_per_chunk: int):
    """بيمشي على صفحات الـ PDF مجموعة مجموعة (زي PAGES_PER_POST)، وبيستخرج صور
    كل مجموعة وقت الحاجة بس، من غير ما يحتفظ بصور المجموعات السابقة في الرام.
    كل مجموعة سابقة بتتحرر من الذاكرة أوتوماتيك بمجرد ما الكود يكمل للمجموعة اللي بعدها،
    لأن معالجتها (رفع + نشر) بتخلص قبل ما نبدأ نستخرج صور المجموعة التالية."""
    total_pages = len(doc)
    for start in range(0, total_pages, pages_per_chunk):
        end = min(start + pages_per_chunk, total_pages)
        images = []
        for page_index in range(start, end):
            page = doc[page_index]
            for img in page.get_images(full=True):
                xref = img[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                ext = base_image.get("ext", "jpg")
                if len(image_bytes) >= MIN_IMAGE_BYTES:
                    images.append({"bytes": image_bytes, "ext": ext})
        yield {"from_page": start + 1, "to_page": end, "images": images}


# أقصى عدد صور في البوست الواحد (فيسبوك بيحدد حد أقصى).
# ده استخدامه كحماية إضافية جوه كل مجموعة صفحات: لو مجموعة الـ 20 صفحة
# نفسها فيها صور أكتر من الحد ده (نادر)، بتتقسم على أكتر من بوست.
MAX_PHOTOS_PER_POST = 100


def upload_unpublished_photo(image_bytes: bytes, ext: str) -> str:
    """يرفع صورة كـ 'غير منشورة' على فيسبوك ويرجع الـ photo_id بتاعها من غير ما تظهر كبوست مستقل."""
    url = f"https://graph.facebook.com/{FB_PAGE_ID}/photos"
    files = {"source": (f"image.{ext}", io.BytesIO(image_bytes))}
    data = {
        "published": "false",
        "access_token": FB_PAGE_ACCESS_TOKEN,
    }
    response = requests.post(url, files=files, data=data, timeout=60)
    _raise_with_fb_error(response)
    return response.json()["id"]


def create_post_with_photos(photo_ids: list[str], caption: str = "") -> dict:
    """يعمل بوست واحد يجمع كل الصور اللي معاها photo_id من الدالة اللي فوق."""
    url = f"https://graph.facebook.com/{FB_PAGE_ID}/feed"
    data = {
        "message": caption,
        "access_token": FB_PAGE_ACCESS_TOKEN,
    }
    for i, photo_id in enumerate(photo_ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": photo_id})
    response = requests.post(url, data=data, timeout=60)
    _raise_with_fb_error(response)
    return response.json()


def _raise_with_fb_error(response: requests.Response) -> None:
    """يطبع رسالة الخطأ الحقيقية من فيسبوك في اللوج قبل ما يرفع الاستثناء."""
    if not response.ok:
        try:
            fb_error = response.json().get("error", {})
        except ValueError:
            fb_error = {"message": response.text}
        logger.error(
            "فيسبوك رفض الطلب | status=%s | code=%s | subcode=%s | message=%s",
            response.status_code,
            fb_error.get("code"),
            fb_error.get("error_subcode"),
            fb_error.get("message"),
        )
        response.raise_for_status()


def _is_user_allowed(user) -> bool:
    return not ALLOWED_USER_IDS or (user is not None and user.id in ALLOWED_USER_IDS)


# عدد محاولات إعادة التحميل لو الاتصال اتقطع في النص
MAX_DOWNLOAD_RETRIES = 4
RETRY_BACKOFF_SECONDS = 3  # بيتضاعف مع كل محاولة فاشلة


def download_pdf_to_tempfile(url: str, progress_callback=None) -> str:
    """يحمّل PDF من رابط ويكتبه على القرص مباشرة (chunk بعد chunk) بدل ما يجمّعه
    في الرام، عشان ملفات كبيرة (لحد 300 ميجا) متستهلكش ذاكرة زيادة أثناء التحميل.
    بيرجع مسار الملف المؤقت. بيتأكد من نوع المحتوى الفعلي (magic bytes) بعد التحميل.

    لو اتبعت progress_callback، بيتنادى بشكل دوري بـ (downloaded_bytes, total_bytes)
    — total_bytes بتبقى None لو السيرفر مش راجع Content-Length.

    لو الاتصال انقطع في النص (مشكلة شبكة شائعة مع الملفات الكبيرة)، بيحاول تاني
    لحد MAX_DOWNLOAD_RETRIES مرات. لو السيرفر بيدعم Range requests (Accept-Ranges: bytes)
    بيكمل من نفس النقطة اللي اتقطعت عندها بدل ما يعيد تحميل الملف من الأول."""
    tmp_path = None
    downloaded = 0
    total_size = None
    supports_range = False
    last_report_time = 0.0
    last_report_bytes = 0
    REPORT_EVERY_SECONDS = 2.0
    REPORT_EVERY_BYTES = 5 * 1024 * 1024  # أو كل 5 ميجا، أيهما أقرب

    attempt = 0
    try:
        while True:
            attempt += 1
            resume = downloaded > 0 and supports_range
            headers = {"Range": f"bytes={downloaded}-"} if resume else {}

            try:
                # (connect timeout, read timeout) — الـ read لازم يكون كبير عشان يكفي
                # تحميل ملفات كبيرة (لحد 300 ميجا) على اتصالات مش سريعة جدًا.
                response = requests.get(url, stream=True, timeout=(15, 300), headers=headers)

                if resume and response.status_code != 206:
                    # السيرفر ماستجابش لطلب الاستئناف، لازم نبدأ من الأول تاني
                    response.close()
                    downloaded = 0
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    tmp_path = None
                    resume = False
                    response = requests.get(url, stream=True, timeout=(15, 300))

                response.raise_for_status()

                if total_size is None:
                    content_length = response.headers.get("Content-Length")
                    if content_length and content_length.isdigit():
                        total_size = int(content_length) + (downloaded if resume else 0)
                    supports_range = response.headers.get("Accept-Ranges", "").lower() == "bytes"

                if tmp_path is None:
                    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="pdf_link_")
                    os.close(fd)

                with open(tmp_path, "ab" if resume else "wb") as f:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > MAX_PDF_DOWNLOAD_BYTES:
                            raise ValueError(
                                f"حجم الملف أكبر من الحد المسموح "
                                f"({MAX_PDF_DOWNLOAD_BYTES // (1024 * 1024)} ميجا)."
                            )
                        f.write(chunk)

                        if progress_callback:
                            now = time.monotonic()
                            if (
                                now - last_report_time >= REPORT_EVERY_SECONDS
                                or downloaded - last_report_bytes >= REPORT_EVERY_BYTES
                            ):
                                last_report_time = now
                                last_report_bytes = downloaded
                                try:
                                    progress_callback(downloaded, total_size)
                                except Exception:
                                    logger.exception("فشل استدعاء progress_callback")

                break  # التحميل خلص بنجاح، اخرج من حلقة إعادة المحاولة

            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as e:
                logger.warning(
                    "انقطع تحميل الملف (محاولة %s/%s): %s", attempt, MAX_DOWNLOAD_RETRIES, e
                )
                if attempt >= MAX_DOWNLOAD_RETRIES:
                    raise ValueError(
                        f"الاتصال بيفصل مع الرابط باستمرار بعد {MAX_DOWNLOAD_RETRIES} محاولات."
                    ) from e
                if progress_callback:
                    try:
                        progress_callback(downloaded, total_size)
                    except Exception:
                        pass
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

        if progress_callback:
            try:
                progress_callback(downloaded, total_size)  # تقرير أخير بعد ما يخلص
            except Exception:
                logger.exception("فشل استدعاء progress_callback")

        with open(tmp_path, "rb") as f:
            header = f.read(4)
        if header != b"%PDF":
            raise ValueError("الرابط ده مش بيرجّع ملف PDF فعلي.")

        return tmp_path
    except Exception:
        # لو حصل أي خطأ نهائي، امسح الملف الجزئي اللي اتحمّل قبل ما نرمي الاستثناء
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


async def process_pdf(update: Update, pdf_path: str) -> None:
    """المنطق المشترك: بيفتح الـ PDF من مساره على القرص، وبيمشي مجموعة صفحات
    (PAGES_PER_POST) في كل مرة — يستخرج صورها، يرفعها، ينشر البوست، وبعدين
    ينتقل للمجموعة التالية. كده صور مجموعة واحدة بس بتفضل في الرام في أي لحظة،
    مش كل صور الملف دفعة واحدة."""
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.exception("فشل فتح الـ PDF")
        await update.message.reply_text(f"معلش، حصل خطأ في قراءة الـ PDF: {e}")
        return

    total_pages = len(doc)
    if total_pages == 0:
        doc.close()
        await update.message.reply_text("الملف ده فاضي من الصفحات.")
        return

    total_groups = (total_pages + PAGES_PER_POST - 1) // PAGES_PER_POST
    await update.message.reply_text(
        f"الملف فيه {total_pages} صفحة.\n"
        f"هيتقسم على {total_groups} مجموعة (كل مجموعة {PAGES_PER_POST} صفحة تقريبًا)، جاري المعالجة والرفع..."
    )

    posts_created = 0
    groups_with_images = 0
    total_images = 0
    total_uploaded = 0
    upload_failed_total = 0
    last_error = None

    try:
        for group_number, group in enumerate(iter_page_chunks(doc, PAGES_PER_POST), start=1):
            if not group["images"]:
                continue  # مجموعة صفحات من غير صور، تجاهلها ومتعملش بوست فاضي

            groups_with_images += 1
            total_images += len(group["images"])
            caption = f"صفحات {group['from_page']} - {group['to_page']}"

            photo_ids = []
            group_failed = 0
            for image in group["images"]:
                try:
                    photo_id = await asyncio.to_thread(
                        upload_unpublished_photo, image["bytes"], image["ext"]
                    )
                    photo_ids.append(photo_id)
                except Exception as e:
                    logger.exception("فشل رفع صورة على فيسبوك (%s)", caption)
                    last_error = str(e)
                    group_failed += 1

            upload_failed_total += group_failed
            total_uploaded += len(photo_ids)

            if not photo_ids:
                continue  # كل صور المجموعة دي فشلت، منعملش بوست فاضي

            for i in range(0, len(photo_ids), MAX_PHOTOS_PER_POST):
                batch = photo_ids[i : i + MAX_PHOTOS_PER_POST]
                try:
                    await asyncio.to_thread(create_post_with_photos, batch, caption)
                    posts_created += 1
                except Exception as e:
                    logger.exception("فشل إنشاء بوست (%s)", caption)
                    last_error = str(e)

            await update.message.reply_text(
                f"تم معالجة المجموعة {group_number}/{total_groups} "
                f"(صفحات {group['from_page']}-{group['to_page']}) ✅"
            )
    finally:
        doc.close()

    if total_images == 0:
        await update.message.reply_text("مفيش صور جوه الملف ده.")
        return

    summary = (
        f"تم رفع {total_uploaded} صورة من أصل {total_images}\n"
        f"تم إنشاء {posts_created} بوست من أصل {groups_with_images} مجموعة صفحات فيها صور ✅"
    )
    if upload_failed_total:
        summary += f"\nفشل رفع {upload_failed_total} صورة"
    if last_error and posts_created == 0:
        summary += f"\n\nآخر خطأ:\n{last_error}"
    await update.message.reply_text(summary)


async def handle_pdf_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يستقبل ملف PDF مرفوع مباشرة (حد تليجرام 20 ميجا للملفات المرفوعة عبر البوت)."""
    user = update.message.from_user
    if not _is_user_allowed(user):
        await update.message.reply_text("معلش، مش متاح ليك تستخدم البوت ده.")
        logger.warning("رفض طلب من يوزر غير مسموح له: %s", user.id if user else None)
        return

    document = update.message.document
    if not document:
        return

    file_name = document.file_name or ""
    is_pdf = (document.mime_type == "application/pdf") or file_name.lower().endswith(".pdf")
    if not is_pdf:
        await update.message.reply_text("ابعتلي ملف PDF بس (أو رابط PDF).")
        return

    await update.message.reply_text("جاري تحميل الملف...")

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="pdf_doc_")
    os.close(fd)
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(tmp_path)
        await process_pdf(update, tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def handle_pdf_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يستقبل رسالة نصية فيها رابط PDF، يحمّله على القرص، وبعدين يعمل نفس المعالجة."""
    user = update.message.from_user
    if not _is_user_allowed(user):
        await update.message.reply_text("معلش، مش متاح ليك تستخدم البوت ده.")
        logger.warning("رفض طلب من يوزر غير مسموح له: %s", user.id if user else None)
        return

    text = update.message.text or ""
    match = URL_PATTERN.search(text)
    if not match:
        return

    url = match.group(0)
    progress_msg = await update.message.reply_text("جاري تحميل الـ PDF من الرابط... 0%")
    loop = asyncio.get_running_loop()

    def on_progress(downloaded: int, total_size):
        downloaded_mb = downloaded / (1024 * 1024)
        if total_size:
            percent = min(100, int(downloaded * 100 / total_size))
            total_mb = total_size / (1024 * 1024)
            text = f"جاري تحميل الـ PDF... {percent}% ({downloaded_mb:.1f} / {total_mb:.1f} ميجا)"
        else:
            # السيرفر مش راجع حجم الملف الكلي، فبنعرض بس اللي اتحمّل لحد دلوقتي
            text = f"جاري تحميل الـ PDF... {downloaded_mb:.1f} ميجا"

        async def _edit():
            try:
                await progress_msg.edit_text(text)
            except Exception:
                pass  # تجاهل أخطاء rate limit البسيطة بتاعة تعديل الرسالة

        asyncio.run_coroutine_threadsafe(_edit(), loop)

    try:
        tmp_path = await asyncio.to_thread(download_pdf_to_tempfile, url, on_progress)
    except Exception as e:
        logger.exception("فشل تحميل الـ PDF من الرابط")
        await progress_msg.edit_text(f"معلش، مقدرتش أحمّل الملف من الرابط: {e}")
        return

    try:
        await progress_msg.edit_text("تم التحميل، جاري المعالجة...")
        await process_pdf(update, tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def main() -> None:
    if not WEBHOOK_URL:
        raise RuntimeError(
            "لازم تحط WEBHOOK_URL في الـ Environment Variables (رابط السيرفس على ريندر)."
        )

    # مكتبة python-telegram-bot بتعتمد جوّاها على asyncio.get_event_loop()
    # وده اتغير سلوكه في بايثون 3.14 وبقى بيرمي RuntimeError لو مفيش loop
    # متظبط للـ thread الحالي مسبقًا. الحل: نظبطه إحنا بأنفسنا قبل ما نستدعي
    # run_webhook()، عشان الكود يشتغل على أي نسخة بايثون من غير ما نعتمد
    # على إعدادات ريندر الخارجية.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf_document))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Entity("url"), handle_pdf_link)
    )

    full_webhook_url = f"{WEBHOOK_URL}/{WEBHOOK_PATH}"
    logger.info("Bot is running as a web service on port %s...", PORT)
    logger.info("Webhook URL: %s", full_webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=full_webhook_url,
    )


if __name__ == "__main__":
    main()
