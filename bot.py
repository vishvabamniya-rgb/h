import os
import re
import asyncio
from typing import Any, List, Optional, Tuple
from io import BytesIO
from datetime import datetime

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

API_BASE = "https://www.mystudyhub.shop/api/kgs"
BATCHES_URL = f"{API_BASE}/batches"
SUBJECTS_URL = f"{API_BASE}/subjects/{{course_id}}"
LESSONS_URL = f"{API_BASE}/lessons/{{subject_id}}"


# ---------- Helpers ----------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def yt_embed_to_watch(url: str) -> str:
    """
    If youtube embed, convert to watch link.
    Otherwise return as-is (works for m3u8/mp4/any URL too).
    """
    if not url:
        return url
    m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]+)", url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return url

async def api_get_json(url: str, params: Optional[dict] = None) -> Any:
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()

def safe_filename(name: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")
    if not s:
        s = "batch"
    return s[:max_len]

def format_lesson_block(lesson: dict) -> str:
    lecture_name = str(lesson.get("name", "Lecture")).strip()
    pub = str(lesson.get("published_at", "")).strip()

    # Any video type is fine (yt/m3u8/mp4/other). We print whatever API gives.
    raw_video = str(lesson.get("video_url") or lesson.get("hd_video_url") or "").strip()
    video = yt_embed_to_watch(raw_video)

    lines = []
    lines.append(f"- {lecture_name}")
    if pub:
        lines.append(f"  Date: {pub}")
    if video:
        lines.append(f"  Video: {video}")
    else:
        lines.append("  Video: (none)")

    pdfs = lesson.get("pdfs")
    if isinstance(pdfs, list) and pdfs:
        for p in pdfs:
            ptitle = str(p.get("title", "PDF")).strip()
            purl = str(p.get("url", "")).strip()
            if purl:
                lines.append(f"  PDF: {ptitle} - {purl}")
    else:
        lines.append("  PDF: (none)")

    return "\n".join(lines)


# ---------- Bot Flow ----------

START_TEXT = (
    "👋 Batch Finder Bot\n\n"
    "✅ Batch *ID* nahi, sirf *Batch Name* type karo.\n"
    "Example: RRB JE / Physics / Khan Sir\n\n"
    "Ab batch name bhejo 👇"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(START_TEXT, parse_mode="Markdown")

async def handle_batch_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    User types batch name keyword -> show matching batches as buttons
    """
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Batch name bhejo (example: RRB JE / Physics).")
        return

    data = await api_get_json(BATCHES_URL)
    courses = data.get("courses", []) if isinstance(data, dict) else []

    q = norm(text)
    matches = []
    for c in courses:
        title = str(c.get("title", ""))
        if q in norm(title):
            matches.append(c)

    if not matches:
        # fallback show first 15
        matches = courses[:15]
        note = "❌ Match nahi mila. Top batches dikh raha hoon — inme se select karo:"
    else:
        note = f"✅ {len(matches)} batches mile. Select karo:"

    show = matches[:20]  # button limit
    keyboard = []
    course_map = {}

    for c in show:
        title = str(c.get("title", "Untitled")).strip()
        cid = c.get("id")
        if cid is None:
            continue
        course_map[str(cid)] = c
        # keep button text smaller
        btn_text = title if len(title) <= 60 else title[:57] + "..."
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"pick_course:{cid}")])

    context.user_data["course_map"] = course_map
    await update.message.reply_text(note, reply_markup=InlineKeyboardMarkup(keyboard))

async def pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    User selects a course -> fetch subjects -> fetch lessons for each subject -> create ONE txt and send
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    _, course_id = data.split(":", 1)

    course_obj = (context.user_data.get("course_map") or {}).get(str(course_id))
    if not course_obj:
        # refetch to resolve title
        all_data = await api_get_json(BATCHES_URL)
        courses = all_data.get("courses", [])
        for c in courses:
            if str(c.get("id")) == str(course_id):
                course_obj = c
                break

    batch_title = str(course_obj.get("title", f"Course {course_id}")).strip() if course_obj else f"Course {course_id}"

    await query.edit_message_text(
        f"📚 Selected Batch:\n{batch_title}\n\n⏳ Full scrape chal raha hai… (subjects + lessons + pdf/video)",
    )

    # 1) subjects
    subj_data = await api_get_json(SUBJECTS_URL.format(course_id=course_id))
    chapters = subj_data.get("chapters", []) if isinstance(subj_data, dict) else []
    if not chapters:
        await query.message.reply_text("❌ Is batch me subjects/chapters nahi mile.")
        return

    # 2) fetch lessons for each subject (limited concurrency)
    sem = asyncio.Semaphore(6)

    async def fetch_subject_lessons(subject: dict) -> Tuple[str, List[dict]]:
        subject_id = subject.get("id")
        subject_name = str(subject.get("name", f"Subject {subject_id}")).strip()

        params = {"sort": "desc", "sortBy": "published_at"}
        async with sem:
            try:
                lessons = await api_get_json(
                    LESSONS_URL.format(subject_id=subject_id),
                    params=params
                )
                if isinstance(lessons, list):
                    return subject_name, lessons
                return subject_name, []
            except Exception:
                return subject_name, []

    results = await asyncio.gather(*[fetch_subject_lessons(s) for s in chapters])

    # counters for caption
    total_videos = 0
    total_pdfs = 0

    # 3) build single TXT content (proper order: subject -> lessons)
    lines: List[str] = []
    lines.append(f"Batch Title: {batch_title}")
    lines.append(f"Course ID: {course_id}")
    lines.append("")
    lines.append("========================================")
    lines.append("")

    for subject_name, lessons in results:
        lines.append(f"Subject: {subject_name}")
        lines.append("----------------------------------------")

        if not lessons:
            lines.append("(No lessons found)")
            lines.append("")
            continue

        for lesson in lessons:
            # count video if exists
            raw_video = str(lesson.get("video_url") or lesson.get("hd_video_url") or "").strip()
            if raw_video:
                total_videos += 1

            pdfs = lesson.get("pdfs")
            if isinstance(pdfs, list) and pdfs:
                total_pdfs += len(pdfs)

            lines.append(format_lesson_block(lesson))
            lines.append("")  # blank line after each lecture

        lines.append("")  # blank line after each subject

    full_text = "\n".join(lines).strip() + "\n"

    # 4) send ONE txt file
    fname = f"{safe_filename(batch_title)}_{course_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    bio = BytesIO(full_text.encode("utf-8"))
    bio.name = fname

    await query.message.reply_document(
        document=bio,
        filename=fname,
        caption=(
            f"✅ Scrape Complete\n"
            f"Batch: {batch_title}\n\n"
            f"📹 Total Videos: {total_videos}\n"
            f"📄 Total PDFs: {total_pdfs}"
        )
    )

    await query.message.reply_text("✅ Done. Dusra batch chahiye to /start karke naya batch name bhejo.")


# ---------- Main ----------

def main():
    token = os.environ.get("BOT_TOKEN", "8761374023:AAENmLmjXyqh0P2gir-59g3lWtJmNthVKXc").strip()
    if not token:
        raise SystemExit("❌ BOT_TOKEN env missing.\nExample:\nexport BOT_TOKEN='123:ABC'")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(pick_course, pattern=r"^pick_course:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_batch_query))

    print("✅ Bot running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
