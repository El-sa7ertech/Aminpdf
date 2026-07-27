"""
بوت تلجرام: يستقبل ملف PDF -> يستخرج كل الصور اللي جواه -> ينشرها على صفحة فيسبوك.

المكتبات المطلوبة:
    pip install python-telegram-bot PyMuPDF requests

الإعدادات المطلوبة (حطها في متغيرات البيئة أو استبدلها تحت مباشرة):
    TELEGRAM_BOT_TOKEN  -> توكن البوت من BotFather
    FB_PAGE_ID          -> ID بتاع صفحة الفيسبوك
    FB_PAGE_ACCESS_TOKEN-> Page Access Token (long-lived) من Graph API

طريقة الحصول على FB_PAGE_ACCESS_TOKEN:
    1. اعمل Facebook App من developers.facebook.com
    2. خد User Access Token من Graph API Explorer بصلاحية pages_manage_posts + pages_read_engagement
    3. بدّله بـ Long-Lived Token
    4. من /me/accounts هتجيبله Page Access Token بتاع صفحتك (ده اللي بيتحط هنا)
"""

import os
import io
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

MIN_IMAGE_BYTES = 3000  # علشان نتجاهل صور صغيرة جدًا (أيقونات/خطوط مدمجة)


def extract_images_from_pdf(pdf_bytes: bytes) -> list[bytes]:
    """يرجع لستة من بايتس الصور المستخرجة من كل صفحات الـ PDF."""
    images = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_index in range(len(doc)):
        page = doc[page_index]
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            if len(image_bytes) >= MIN_IMAGE_BYTES:
                images.append(image_bytes)
    doc.close()
    return images


def post_image_to_facebook_page(image_bytes: bytes, caption: str = "") -> dict:
    """ينشر صورة واحدة على صفحة الفيسبوك عن طريق Graph API."""
    url = f"https://graph.facebook.com/{FB_PAGE_ID}/photos"
    files = {"source": ("image.jpg", io.BytesIO(image_bytes))}
    data = {"caption": caption, "access_token": FB_PAGE_ACCESS_TOKEN}
    response = requests.post(url, files=files, data=data, timeout=60)
    response.raise_for_status()
    return response.json()


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    await update.message.reply_text(f"لقيت {len(images)} صورة، جاري النشر على فيسبوك...")

    posted = 0
    failed = 0
    for image_bytes in images:
        try:
            post_image_to_facebook_page(image_bytes)
            posted += 1
        except Exception as e:
            logger.exception("فشل نشر صورة على فيسبوك")
            failed += 1

    await update.message.reply_text(f"تم النشر: {posted} صورة ✅\nفشل: {failed} صورة")


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
