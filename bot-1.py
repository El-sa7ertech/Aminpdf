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
    - Start Command:  python bot-1-webhook.py
    - لازم تحط WEBHOOK_URL = رابط السيرفس نفسه بعد ما ينعمل Deploy أول مرة
"""

import os
import io
import re
import json
import asyncio
import logging
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
# برابط بيرجّع ملف ضخم يفجر الرام. القيمة الافتراضية هنا 50 ميجا.
MAX_PDF_DOWNLOAD_BYTES = int(os.environ.get("MAX_PDF_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))

# عدد صفحات الـ PDF اللي بتتجمع صورها في بوست واحد.
# مثال: PDF من 100 صفحة مع PAGES_PER_POST = 20 هيتقسم على 5 بوستات.
PAGES_PER_POST = int(os.environ.get("PAGES_PER_POST", "20"))


def extract_images_by_page(pdf_bytes: bytes) -> list[list[dict]]:
    """يرجع لستة فيها لستة صور لكل صفحة، بنفس ترتيب صفحات الـ PDF.
    يعني index 0 = صور الصفحة الأولى، index 1 = صور الصفحة التانية، وهكذا."""
    pages_images: list[list[dict]] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_index in range(len(doc)):
        page = doc[page_index]
        page_images = []
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            ext = base_image.get("ext", "jpg")
            if len(image_bytes) >= MIN_IMAGE_BYTES:
                page_images.append({"bytes": image_bytes, "ext": ext})
        pages_images.append(page_images)
    doc.close()
    return pages_images


def chunk_pages(pages_images: list[list[dict]], pages_per_chunk: int):
    """يقسم صور الصفحات لمجموعات، كل مجموعة بتمثل مدى صفحات (from_page, to_page, images).
    from_page و to_page بيبدأوا من 1 (مش صفر) عشان تبقى واضحة للمستخدم."""
    total_pages = len(pages_images)
    for start in range(0, total_pages, pages_per_chunk):
        end = min(start + pages_per_chunk, total_pages)
        images = [img for page in pages_images[start:end] for img in page]
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


def download_pdf_from_url(url: str) -> bytes:
    """يحمّل PDF من رابط مع حد أقصى للحجم عشان يمنع تحميل ملفات ضخمة على الرام.
    بيتأكد من نوع المحتوى الفعلي (magic bytes) بدل ما يعتمد على الامتداد بس."""
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()

    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_PDF_DOWNLOAD_BYTES:
            raise ValueError(
                f"حجم الملف أكبر من الحد المسموح ({MAX_PDF_DOWNLOAD_BYTES // (1024 * 1024)} ميجا)."
            )
        chunks.append(chunk)

    data = b"".join(chunks)
    if not data.startswith(b"%PDF"):
        raise ValueError("الرابط ده مش بيرجّع ملف PDF فعلي.")
    return data


async def process_pdf(update: Update, pdf_bytes: bytes) -> None:
    """المنطق المشترك: استخراج الصور، تقسيمها، رفعها ونشرها على فيسبوك.
    مستخدمة سواء كان مصدر الـ PDF ملف مرفوع أو رابط."""
    try:
        pages_images = extract_images_by_page(pdf_bytes)
    except Exception as e:
        logger.exception("فشل استخراج الصور")
        await update.message.reply_text(f"معلش، حصل خطأ في قراءة الـ PDF: {e}")
        return

    total_pages = len(pages_images)
    total_images = sum(len(p) for p in pages_images)

    if total_images == 0:
        await update.message.reply_text("مفيش صور جوه الملف ده.")
        return

    groups = list(chunk_pages(pages_images, PAGES_PER_POST))
    # تجاهل أي مجموعة صفحات مفيش فيها صور خالص (مفيش داعي نعمل بوست فاضي)
    groups = [g for g in groups if g["images"]]

    await update.message.reply_text(
        f"الملف فيه {total_pages} صفحة و {total_images} صورة.\n"
        f"هيتقسم على {len(groups)} بوست (كل بوست بصور {PAGES_PER_POST} صفحة تقريبًا)، جاري الرفع..."
    )

    posts_created = 0
    total_uploaded = 0
    upload_failed_total = 0
    last_error = None

    for group in groups:
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
            # كل صور المجموعة دي فشلت، منعملش بوست فاضي ونكمل للمجموعة اللي بعدها
            continue

        # لو مجموعة الصفحات دي لوحدها فيها صور أكتر من حد فيسبوك للبوست الواحد
        for i in range(0, len(photo_ids), MAX_PHOTOS_PER_POST):
            batch = photo_ids[i : i + MAX_PHOTOS_PER_POST]
            try:
                await asyncio.to_thread(create_post_with_photos, batch, caption)
                posts_created += 1
            except Exception as e:
                logger.exception("فشل إنشاء بوست (%s)", caption)
                last_error = str(e)

    summary = (
        f"تم رفع {total_uploaded} صورة من أصل {total_images}\n"
        f"تم إنشاء {posts_created} بوست من أصل {len(groups)} مجموعة صفحات ✅"
    )
    if upload_failed_total:
        summary += f"\nفشل رفع {upload_failed_total} صورة"
    if last_error and posts_created == 0:
        summary += f"\n\nآخر خطأ:\n{last_error}"
    await update.message.reply_text(summary)


async def handle_pdf_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يستقبل ملف PDF مرفوع مباشرة."""
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

    await update.message.reply_text("جاري استخراج الصور من الملف...")

    tg_file = await context.bot.get_file(document.file_id)
    pdf_bytes = await tg_file.download_as_bytearray()

    await process_pdf(update, bytes(pdf_bytes))


async def handle_pdf_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يستقبل رسالة نصية فيها رابط PDF، يحمّله، وبعدين يعمل نفس المعالجة."""
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
    await update.message.reply_text("جاري تحميل الـ PDF من الرابط...")

    try:
        pdf_bytes = await asyncio.to_thread(download_pdf_from_url, url)
    except Exception as e:
        logger.exception("فشل تحميل الـ PDF من الرابط")
        await update.message.reply_text(f"معلش، مقدرتش أحمّل الملف من الرابط: {e}")
        return

    await update.message.reply_text("تم التحميل، جاري استخراج الصور...")
    await process_pdf(update, pdf_bytes)


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
