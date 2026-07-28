"""
بوت تلجرام: يستقبل ملف PDF -> يستخرج كل الصور اللي جواه -> ينشرها على صفحة فيسبوك.

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


def extract_images_from_pdf(pdf_bytes: bytes) -> list[dict]:
    """يرجع لستة من dicts فيها bytes الصورة والامتداد الحقيقي بتاعها."""
    images = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_index in range(len(doc)):
        page = doc[page_index]
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            ext = base_image.get("ext", "jpg")
            if len(image_bytes) >= MIN_IMAGE_BYTES:
                images.append({"bytes": image_bytes, "ext": ext})
    doc.close()
    return images


# أقصى عدد صور في البوست الواحد (فيسبوك بيحدد حد أقصى، فلو الصور أكتر بنقسمهم على أكتر من بوست)
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


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.message.from_user
    if ALLOWED_USER_IDS and (user is None or user.id not in ALLOWED_USER_IDS):
        await update.message.reply_text("معلش، مش متاح ليك تستخدم البوت ده.")
        logger.warning("رفض طلب من يوزر غير مسموح له: %s", user.id if user else None)
        return

    document = update.message.document
    if not document:
        return

    file_name = document.file_name or ""
    is_pdf = (document.mime_type == "application/pdf") or file_name.lower().endswith(".pdf")
    if not is_pdf:
        await update.message.reply_text("ابعتلي ملف PDF بس.")
        return

    await update.message.reply_text("جاري استخراج الصور من الملف...")

    tg_file = await context.bot.get_file(document.file_id)
    pdf_bytes = await tg_file.download_as_bytearray()

    try:
        images = extract_images_from_pdf(bytes(pdf_bytes))
    except Exception as e:
        logger.exception("فشل استخراج الصور")
        await update.message.reply_text(f"معلش، حصل خطأ في قراءة الـ PDF: {e}")
        return

    if not images:
        await update.message.reply_text("مفيش صور جوه الملف ده.")
        return

    await update.message.reply_text(f"لقيت {len(images)} صورة، جاري رفعها على فيسبوك...")

    photo_ids = []
    upload_failed = 0
    last_error = None
    for image in images:
        try:
            photo_id = await asyncio.to_thread(
                upload_unpublished_photo, image["bytes"], image["ext"]
            )
            photo_ids.append(photo_id)
        except Exception as e:
            logger.exception("فشل رفع صورة على فيسبوك")
            last_error = str(e)
            upload_failed += 1

    if not photo_ids:
        summary = f"فشل رفع كل الصور ({upload_failed}) ولم يتم النشر."
        if last_error:
            summary += f"\n\nآخر خطأ:\n{last_error}"
        await update.message.reply_text(summary)
        return

    # نقسم الصور على أكتر من بوست لو عددها أكبر من الحد الأقصى المسموح في البوست الواحد
    posts_created = 0
    for i in range(0, len(photo_ids), MAX_PHOTOS_PER_POST):
        batch = photo_ids[i : i + MAX_PHOTOS_PER_POST]
        try:
            await asyncio.to_thread(create_post_with_photos, batch)
            posts_created += 1
        except Exception as e:
            logger.exception("فشل إنشاء البوست المجمّع")
            last_error = str(e)

    summary = (
        f"تم رفع {len(photo_ids)} صورة من أصل {len(images)}\n"
        f"تم إنشاء {posts_created} بوست يجمعهم ✅"
    )
    if upload_failed:
        summary += f"\nفشل رفع {upload_failed} صورة"
    if last_error and posts_created == 0:
        summary += f"\n\nآخر خطأ:\n{last_error}"
    await update.message.reply_text(summary)


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
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))

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
