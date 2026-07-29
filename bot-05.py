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
    SECOND_ACCOUNT_CHAT_ID -> (اختياري) chat_id بتاع حسابك التاني على تليجرام.
                            لو موجود، البوت هيبعتله نسخة PDF من كل مجموعة صفحات
                            قبل ما يرفع صورها على فيسبوك. سيبها فاضية لو مش عايز الخاصية دي.
                            عشان تجيبه: ابعت أي رسالة للبوت من الحساب ده، وبعدين افتح
                            https://api.telegram.org/bot<TOKEN>/getUpdates وهتلاقي
                            الرقم جوه "chat":{"id": ...}

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

# chat_id بتاع حسابك التاني اللي هيوصله نسخة PDF من كل مجموعة صفحات
# قبل ما يترفع صورها على فيسبوك. سيبها فاضية لو مش عايز الخاصية دي.
SECOND_ACCOUNT_CHAT_ID = os.environ.get("SECOND_ACCOUNT_CHAT_ID", "").strip()

# حد تليجرام لحجم أي ملف بيتبعت من البوت (50 ميجا)
TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


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


def build_pdf_chunk_bytes(doc: "fitz.Document", from_page: int, to_page: int) -> bytes:
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
    """بيبعت نطاق الصفحات كملف PDF لحسابك التاني (SECOND_ACCOUNT_CHAT_ID)،
    لو الإعداد ده مش فاضي. بيتنادى قبل ما يترفع صور المجموعة على فيسبوك."""
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


# أقصى عدد صور في البوست الواحد (فيسبوك بيحدد حد أقصى).
# ده استخدامه كحماية إضافية جوه كل مجموعة صفحات: لو مجموعة الـ 20 صفحة
# نفسها فيها صور أكتر من الحد ده (نادر)، بتتقسم على أكتر من بوست.
MAX_PHOTOS_PER_POST = 100


# --- حماية من Rate Limit بتاع Graph API ---
# لو فيسبوك رجّع أي كود من دول (أو HTTP 429)، معناه إحنا بنبعت طلبات كتير
# بسرعة وهو بيرفض مؤقتًا. الأكواد دي معروفة ومكررة في توثيق Graph API:
#   4   -> Application request limit reached (rate limit عام على مستوى الـ app)
#   17  -> User request limit reached
#   32  -> Page request limit reached
#   613 -> Calls to this API have exceeded the rate limit
FB_RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613}

MAX_FB_RETRIES = 5
FB_RETRY_BACKOFF_SECONDS = 5  # بيتضاعف مع كل محاولة (5, 10, 20, 40, ...)


class FacebookRateLimitError(Exception):
    """بيترمى لما فيسبوك يرفض الطلب بسبب rate limit حتى بعد كل المحاولات."""


def _fb_error_info(response: requests.Response) -> dict:
    try:
        return response.json().get("error", {})
    except ValueError:
        return {"message": response.text}


def _is_fb_rate_limit(response: requests.Response, fb_error: dict) -> bool:
    if response.status_code == 429:
        return True
    code = fb_error.get("code")
    return code in FB_RATE_LIMIT_ERROR_CODES


def _fb_request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """بيعمل نفس طلب requests العادي، لكن لو فيسبوك رجّع rate limit بيستنى وبيعيد
    المحاولة (backoff تصاعدي)، وبيحترم هيدر Retry-After لو فيسبوك بعته."""
    attempt = 0
    while True:
        attempt += 1
        response = requests.request(method, url, **kwargs)

        if response.ok:
            return response

        fb_error = _fb_error_info(response)

        if _is_fb_rate_limit(response, fb_error):
            if attempt >= MAX_FB_RETRIES:
                logger.error(
                    "فيسبوك rate limit مستمر بعد %s محاولات | code=%s | message=%s",
                    MAX_FB_RETRIES,
                    fb_error.get("code"),
                    fb_error.get("message"),
                )
                raise FacebookRateLimitError(
                    fb_error.get("message", "تم تجاوز حد الطلبات المسموح به من فيسبوك.")
                )

            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait_seconds = int(retry_after)
            else:
                wait_seconds = FB_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))

            logger.warning(
                "فيسبوك رجّع rate limit (محاولة %s/%s) | code=%s | هنستنى %s ثانية",
                attempt,
                MAX_FB_RETRIES,
                fb_error.get("code"),
                wait_seconds,
            )
            time.sleep(wait_seconds)
            continue

        # خطأ تاني مش rate limit -> يتعامل بيه زي ما كان (يتسجل ويترمى فورًا)
        _log_fb_error(response, fb_error)
        response.raise_for_status()


def upload_unpublished_photo(image_bytes: bytes, ext: str) -> str:
    """يرفع صورة كـ 'غير منشورة' على فيسبوك ويرجع الـ photo_id بتاعها من غير ما تظهر كبوست مستقل."""
    url = f"https://graph.facebook.com/{FB_PAGE_ID}/photos"
    files = {"source": (f"image.{ext}", io.BytesIO(image_bytes))}
    data = {
        "published": "false",
        "access_token": FB_PAGE_ACCESS_TOKEN,
    }
    response = _fb_request_with_retry("POST", url, files=files, data=data, timeout=60)
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
    response = _fb_request_with_retry("POST", url, data=data, timeout=60)
    return response.json()


def _log_fb_error(response: requests.Response, fb_error: dict) -> None:
    """يطبع رسالة الخطأ الحقيقية من فيسبوك في اللوج."""
    logger.error(
        "فيسبوك رفض الطلب | status=%s | code=%s | subcode=%s | message=%s",
        response.status_code,
        fb_error.get("code"),
        fb_error.get("error_subcode"),
        fb_error.get("message"),
    )


def _raise_with_fb_error(response: requests.Response) -> None:
    """محتفظ بيها لأي استخدام قديم: بتطبع الخطأ وترمي استثناء عادي."""
    if not response.ok:
        _log_fb_error(response, _fb_error_info(response))
        response.raise_for_status()


def _is_user_allowed(user) -> bool:
    return not ALLOWED_USER_IDS or (user is not None and user.id in ALLOWED_USER_IDS)


# عدد محاولات إعادة التحميل لو الاتصال اتقطع في النص
MAX_DOWNLOAD_RETRIES = 4
RETRY_BACKOFF_SECONDS = 3  # بيتضاعف مع كل محاولة فاشلة

# بعض السيرفرات بترفض الطلبات اللي مالهاش User-Agent شبه متصفح حقيقي
# (بتفتكرها بوت/سكريبت وترفضها بـ 400/403). بنبعت هيدر شبه متصفح عادي.
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


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
            headers = dict(DOWNLOAD_HEADERS)
            if resume:
                headers["Range"] = f"bytes={downloaded}-"

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


async def process_pdf(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pdf_path: str
) -> None:
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
            # إرسال المجموعة كـ PDF للحساب التاني بيحصل لكل مجموعة صفحات دايمًا،
            # حتى لو المجموعة دي مفيهاش صور جوه هتترفع على فيسبوك.
            if SECOND_ACCOUNT_CHAT_ID:
                try:
                    chunk_bytes = await asyncio.to_thread(
                        build_pdf_chunk_bytes, doc, group["from_page"], group["to_page"]
                    )
                    await send_chunk_to_second_account(
                        context, chunk_bytes, group["from_page"], group["to_page"]
                    )
                except Exception:
                    logger.exception(
                        "فشل بناء/إرسال نسخة PDF من المجموعة %s-%s للحساب التاني",
                        group["from_page"],
                        group["to_page"],
                    )

            if not group["images"]:
                continue  # مجموعة صفحات من غير صور، تجاهلها ومتعملش بوست فاضي على فيسبوك

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
        await process_pdf(update, context, tmp_path)
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
