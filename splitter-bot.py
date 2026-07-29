"""
بوت تلجرام: يستقبل ملف PDF (أو رابط PDF) -> يقسمه لمجموعات صفحات -> يرجع كل
مجموعة كملف PDF مستقل لنفس الشخص اللي بعت الملف، في نفس المحادثة.

بوت منفصل تمامًا عن بوت النشر على فيسبوك (bot-1.py) — مفيش أي تكامل مع
فيسبوك هنا خالص، هو بس تقسيم وإرسال.

البوت بيقبل نوعين من المدخلات:
    1. ملف PDF مرفوع مباشرة كـ Document.
    2. رسالة نصية فيها رابط (هيتأكد البوت من نوع المحتوى الفعلي بعد
       التحميل بدل ما يعتمد على الامتداد بس).

نسخة Webhook — مخصصة للتشغيل على Render كـ Web Service (مش Background Worker).

المكتبات المطلوبة (requirements.txt):
    python-telegram-bot[webhooks]
    PyMuPDF
    requests

الإعدادات المطلوبة (Environment Variables في Render):
    TELEGRAM_BOT_TOKEN   -> توكن البوت من BotFather (لازم يكون توكن مختلف عن
                            بوت الفيسبوك، لأن كل بوت على تليجرام له توكن خاص بيه)
    WEBHOOK_URL          -> رابط السيرفس على ريندر، مثال:
                            https://your-service-name.onrender.com
    ALLOWED_USER_IDS     -> (اختياري) أرقام يوزرات تليجرام المسموح لهم، مفصولة بفاصلة
                            مثال: 123456789,987654321
                            لو سبتها فاضية، أي حد هيقدر يستخدم البوت.
    PAGES_PER_GROUP      -> (اختياري) عدد الصفحات في كل ملف PDF فرعي (افتراضي 20)
    MAX_PDF_DOWNLOAD_BYTES -> (اختياري) أقصى حجم لملف PDF بيتحمل من رابط (افتراضي 300 ميجا)
    SECOND_ACCOUNT_CHAT_ID -> (اختياري) chat_id بتاع حساب تاني على تليجرام.
                            لو موجود، البوت هيبعتله نسخة من كل ملف فرعي زي ما بيتبعت
                            للشخص الأصلي. سيبها فاضية لو مش عايز الخاصية دي.
                            عشان تجيبه: ابعت أي رسالة للبوت من الحساب ده، وبعدين افتح
                            https://api.telegram.org/bot<TOKEN>/getUpdates وهتلاقي
                            الرقم جوه "chat":{"id": ...}

إعداد Render:
    - Service type: Web Service (مش Background Worker)
    - Build Command:  pip install -r requirements.txt
    - Start Command:  python splitter-bot.py
    - لازم تحط WEBHOOK_URL = رابط السيرفس نفسه بعد ما ينعمل Deploy أول مرة
"""

import os
import io
import re
import gc
import time
import asyncio
import logging
import resource
import tempfile
import requests
import fitz  # PyMuPDF

from telegram import Update
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.request import HTTPXRequest
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

# ريندر بيدي البورت في متغير البيئة PORT تلقائيًا
PORT = int(os.environ.get("PORT", "10000"))

# لازم يكون رابط السيرفس بتاعك على ريندر (من غير / في الآخر)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")

# مسار سري بسيط للـ webhook، بيتبني من التوكن نفسه عشان محدش يقدر يخمنه
WEBHOOK_PATH = TELEGRAM_BOT_TOKEN


class _TokenRedactionFilter(logging.Filter):
    """بيشيل التوكن بتاع البوت من أي رسالة لوج قبل ما تتطبع، سواء الرسالة دي
    جاية من كودنا أو من مكتبات زي httpx/telegram اللي بتطبع الـ URL كامل
    وقت أي طلب لـ Telegram API (زي setWebhook, editMessageText, ...)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != "PUT_YOUR_TELEGRAM_TOKEN_HERE":
            if isinstance(record.msg, str) and TELEGRAM_BOT_TOKEN in record.msg:
                record.msg = record.msg.replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")
            if record.args:
                record.args = tuple(
                    arg.replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")
                    if isinstance(arg, str) and TELEGRAM_BOT_TOKEN in arg
                    else arg
                    for arg in record.args
                )
        return True


_token_filter = _TokenRedactionFilter()
logging.getLogger().addFilter(_token_filter)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_token_filter)

# قائمة اليوزرات المسموح لهم يستخدموا البوت (لو فاضية = الكل مسموح، مش موصى بيه)
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

# حد أقصى لحجم أي PDF بيتحمل من رابط (بالبايت)، عشان مايبقاش فيه استغلال
# برابط بيرجّع ملف ضخم يفجر الرام. القيمة الافتراضية هنا 300 ميجا.
MAX_PDF_DOWNLOAD_BYTES = int(os.environ.get("MAX_PDF_DOWNLOAD_BYTES", str(300 * 1024 * 1024)))

# عدد الصفحات في كل ملف PDF فرعي.
# مثال: PDF من 100 صفحة مع PAGES_PER_GROUP = 20 هيتقسم على 5 ملفات.
PAGES_PER_GROUP = int(os.environ.get("PAGES_PER_GROUP", "20"))

# حد تليجرام لحجم أي ملف بيتبعت من البوت (50 ميجا)
TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

# chat_id بتاع حساب تاني اللي هيوصله نسخة من كل ملف فرعي بعد ما يترسل للشخص الأصلي.
# سيبها فاضية لو مش عايز الخاصية دي.
SECOND_ACCOUNT_CHAT_ID = os.environ.get("SECOND_ACCOUNT_CHAT_ID", "").strip()

# عدد محاولات إعادة التحميل لو الاتصال اتقطع في النص
MAX_DOWNLOAD_RETRIES = 4
RETRY_BACKOFF_SECONDS = 3  # بيتضاعف مع كل محاولة فاشلة

# بعض السيرفرات بترفض الطلبات اللي مالهاش User-Agent شبه متصفح حقيقي
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def _is_user_allowed(user) -> bool:
    return not ALLOWED_USER_IDS or (user is not None and user.id in ALLOWED_USER_IDS)


def download_pdf_to_tempfile(url: str, progress_callback=None) -> str:
    """يحمّل PDF من رابط ويكتبه على القرص مباشرة (chunk بعد chunk)، ويتأكد
    من نوع المحتوى الفعلي (magic bytes) بعد التحميل. بيدعم استئناف التحميل
    (Range requests) لو الاتصال انقطع، ويعيد المحاولة لحد MAX_DOWNLOAD_RETRIES."""
    tmp_path = None
    downloaded = 0
    total_size = None
    supports_range = False
    last_report_time = 0.0
    last_report_bytes = 0
    REPORT_EVERY_SECONDS = 2.0
    REPORT_EVERY_BYTES = 5 * 1024 * 1024

    attempt = 0
    try:
        while True:
            attempt += 1
            resume = downloaded > 0 and supports_range
            headers = dict(DOWNLOAD_HEADERS)
            if resume:
                headers["Range"] = f"bytes={downloaded}-"

            try:
                response = requests.get(url, stream=True, timeout=(15, 300), headers=headers)

                if resume and response.status_code != 206:
                    response.close()
                    downloaded = 0
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    tmp_path = None
                    resume = False
                    response = requests.get(
                        url, stream=True, timeout=(15, 300), headers=DOWNLOAD_HEADERS
                    )

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

                break

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
                progress_callback(downloaded, total_size)
            except Exception:
                logger.exception("فشل استدعاء progress_callback")

        with open(tmp_path, "rb") as f:
            header = f.read(4)
        if header != b"%PDF":
            raise ValueError("الرابط ده مش بيرجّع ملف PDF فعلي.")

        return tmp_path
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _log_memory_usage(context_label: str) -> None:
    """بيسجل بالـ logs أعلى استهلاك رام وصلت له العملية لحد دلوقتي (بالميجا).
    مفيد جدًا عشان تشوف بالـ logs بتاعة Render هل الرام بتزيد بشكل متراكم مع
    كل جزء ولا لأ (لو بتزيد باستمرار من غير ما تنزل، ده مؤشر على مشكلة
    OOM محتملة قبل ما تحصل فعليًا)."""
    try:
        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        logger.info("استهلاك الرام (peak) بعد %s: %.1f ميجا", context_label, peak_kb / 1024)
    except Exception:
        pass



    """بيبني ملف PDF مستقل يحتوي بس على نطاق الصفحات ده (from_page/to_page أرقام
    مبنية على 1)، وبيرجعه كـ bytes جاهزة للإرسال. مبيلمسش الملف الأصلي."""
    chunk_doc = fitz.open()
    try:
        chunk_doc.insert_pdf(doc, from_page=from_page - 1, to_page=to_page - 1)
        return chunk_doc.tobytes()
    finally:
        chunk_doc.close()


async def send_chunk_to_second_account(
    context: ContextTypes.DEFAULT_TYPE,
    pdf_bytes: bytes,
    from_page: int,
    to_page: int,
) -> None:
    """بيبعت نسخة من ملف الصفحات دي لحسابك التاني (SECOND_ACCOUNT_CHAT_ID)،
    لو الإعداد ده مش فاضي. بيتنادى بعد ما يترسل الملف للشخص الأصلي."""
    if not SECOND_ACCOUNT_CHAT_ID:
        return

    if len(pdf_bytes) > TELEGRAM_MAX_DOCUMENT_BYTES:
        logger.warning(
            "مجموعة الصفحات %s-%s حجمها أكبر من حد تليجرام (50 ميجا)، مش هتترسل للحساب التاني",
            from_page,
            to_page,
        )
        return

    try:
        await context.bot.send_document(
            chat_id=SECOND_ACCOUNT_CHAT_ID,
            document=io.BytesIO(pdf_bytes),
            filename=f"pages_{from_page}-{to_page}.pdf",
            caption=f"صفحات {from_page} - {to_page}",
        )
    except Exception:
        logger.exception(
            "فشل إرسال مجموعة الصفحات %s-%s للحساب التاني", from_page, to_page
        )


# عدد محاولات إعادة إرسال الملف لو حصل Timeout أثناء الرفع لتليجرام
MAX_SEND_RETRIES = 3
SEND_RETRY_BACKOFF_SECONDS = 5  # بيتضاعف مع كل محاولة (5, 10, ...)


async def _reply_document_with_retry(
    update: Update, pdf_bytes: bytes, filename: str, caption: str
):
    """بيبعت الملف للمستخدم، ولو حصل Timeout (شائع مع الملفات الكبيرة أو النت
    البطيء) بيعيد المحاولة قبل ما يستسلم."""
    attempt = 0
    while True:
        attempt += 1
        try:
            await update.message.reply_document(
                document=io.BytesIO(pdf_bytes),
                filename=filename,
                caption=caption,
            )
            return
        except RetryAfter as e:
            # تليجرام نفسه بيقول لازم تستنى كام ثانية بالظبط قبل ما تعيد المحاولة
            wait_seconds = e.retry_after + 1
            logger.warning(
                "تليجرام طلب الانتظار (flood control) في إرسال %s، هنستنى %s ثانية",
                filename,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            continue
        except (TimedOut, NetworkError) as e:
            if attempt >= MAX_SEND_RETRIES:
                raise
            wait_seconds = SEND_RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "تايم أوت في إرسال %s (محاولة %s/%s)، هنستنى %s ثانية ونعيد",
                filename,
                attempt,
                MAX_SEND_RETRIES,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            continue


async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, pdf_path: str) -> None:
    """بيفتح الـ PDF من مساره على القرص، ويقسمه لمجموعات صفحات (PAGES_PER_GROUP)،
    وبيبعت كل مجموعة كملف PDF مستقل رد في نفس المحادثة اللي جت منها."""
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

    _log_memory_usage("فتح الملف (قبل بداية التقسيم)")

    total_groups = (total_pages + PAGES_PER_GROUP - 1) // PAGES_PER_GROUP
    await update.message.reply_text(
        f"الملف فيه {total_pages} صفحة.\n"
        f"هيتقسم على {total_groups} ملف (كل ملف {PAGES_PER_GROUP} صفحة تقريبًا)، جاري القص والإرسال..."
    )

    sent_count = 0
    failed_count = 0
    last_error = None

    try:
        for group_number in range(1, total_groups + 1):
            from_page = (group_number - 1) * PAGES_PER_GROUP + 1
            to_page = min(group_number * PAGES_PER_GROUP, total_pages)

            try:
                chunk_bytes = await asyncio.to_thread(
                    build_pdf_chunk_bytes, doc, from_page, to_page
                )
            except Exception as e:
                logger.exception("فشل قص نطاق الصفحات %s-%s", from_page, to_page)
                failed_count += 1
                last_error = str(e)
                continue

            if len(chunk_bytes) > TELEGRAM_MAX_DOCUMENT_BYTES:
                logger.warning(
                    "نطاق الصفحات %s-%s حجمه أكبر من حد تليجرام (50 ميجا)، هيتم تجاهله",
                    from_page,
                    to_page,
                )
                failed_count += 1
                last_error = (
                    f"مجموعة الصفحات {from_page}-{to_page} حجمها أكبر من 50 ميجا "
                    "(حد تليجرام الأقصى لأي ملف)."
                )
                continue

            try:
                await _reply_document_with_retry(
                    update,
                    chunk_bytes,
                    filename=f"pages_{from_page}-{to_page}.pdf",
                    caption=f"صفحات {from_page} - {to_page} ({group_number}/{total_groups})",
                )
                sent_count += 1
            except Exception as e:
                logger.exception("فشل إرسال نطاق الصفحات %s-%s", from_page, to_page)
                failed_count += 1
                last_error = str(e)
                continue

            await send_chunk_to_second_account(context, chunk_bytes, from_page, to_page)

            try:
                await update.message.reply_text(
                    f"تم إرسال الملف {group_number}/{total_groups} "
                    f"(صفحات {from_page}-{to_page}) ✅"
                )
            except RetryAfter as e:
                logger.warning(
                    "فلود كنترول على رسالة التأكيد، هنستنى %s ثانية", e.retry_after
                )
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                # رسالة التأكيد مش أساسية، لو فشلت منكملش نوقف عملية الإرسال كلها بسببها
                logger.exception(
                    "فشل إرسال رسالة تأكيد الجزء %s-%s، هنكمل عادي", from_page, to_page
                )

            # تنظيف ذاكرة صريح بعد كل جزء + تسجيل استهلاك الرام الحالي بالـ logs
            # عشان تقدر تتابع على Render هل فيه تراكم رام مع الأجزاء ولا لأ
            del chunk_bytes
            gc.collect()
            _log_memory_usage(f"الجزء {group_number}/{total_groups}")

            # تأخير بسيط بين كل جزء وبعده عشان نتجنب حد الفلود بتاع تليجرام
            # (خصوصًا مع ملفات فيها أجزاء كتير زي دي)
            await asyncio.sleep(1.2)
    finally:
        doc.close()

    summary = f"تم إرسال {sent_count} ملف من أصل {total_groups} ✅"
    if failed_count:
        summary += f"\nفشل إرسال {failed_count} ملف"
        if last_error:
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
        await process_pdf(update, context, tmp_path)
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
            text = f"جاري تحميل الـ PDF... {downloaded_mb:.1f} ميجا"

        async def _edit():
            try:
                await progress_msg.edit_text(text)
            except Exception:
                pass

        asyncio.run_coroutine_threadsafe(_edit(), loop)

    try:
        tmp_path = await asyncio.to_thread(download_pdf_to_tempfile, url, on_progress)
    except Exception as e:
        logger.exception("فشل تحميل الـ PDF من الرابط")
        await progress_msg.edit_text(f"معلش، مقدرتش أحمّل الملف من الرابط: {e}")
        return

    try:
        await progress_msg.edit_text("تم التحميل، جاري القص والإرسال...")
        await process_pdf(update, context, tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def main() -> None:
    if not WEBHOOK_URL:
        raise RuntimeError(
            "لازم تحط WEBHOOK_URL في الـ Environment Variables (رابط السيرفس على ريندر)."
        )

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=120,
        write_timeout=120,
        pool_timeout=30,
    )
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).build()
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
