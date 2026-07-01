"""
OBD Diagnostic Agent — Telegram Bot

Analysiert Fahrzeugfehler (OBD, CAN, FlexRay, Ethernet/DoIP, LIN) per:
- Text: Hersteller + Modell + Fehlercode(s)
- Screenshot: OBD-Auslesegerät, Diagnose-Software

Commands:
  /start  — Erklärung
  /reset  — Konversation zurücksetzen (neues Fahrzeug)
"""

import base64
import logging
import os
import configparser
from pathlib import Path

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_cfg = configparser.ConfigParser()
_cfg.read(Path(__file__).parent / "config.ini", encoding="utf-8")

BOT_TOKEN     = os.environ.get("BOT_TOKEN")           or _cfg.get("telegram",  "bot_token",       fallback="")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")   or _cfg.get("anthropic", "api_key",         fallback="")
ALLOWED_CHAT  = int(os.environ.get("ALLOWED_CHAT_ID") or _cfg.get("telegram",  "allowed_chat_id", fallback="0"))

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.txt").read_text(encoding="utf-8")

# ── Client ────────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

# ── Konversations-History (pro Chat, in-memory) ───────────────────────────────
_history: dict[int, list] = {}

CONTEXT_MESSAGES = 20

def _get_history(chat_id: int) -> list:
    return _history.setdefault(chat_id, [])

def _add(chat_id: int, role: str, content) -> None:
    h = _get_history(chat_id)
    h.append({"role": role, "content": content})
    if len(h) > CONTEXT_MESSAGES:
        _history[chat_id] = h[-CONTEXT_MESSAGES:]

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.text for b in content if hasattr(b, "text") and b.text).strip()

# ── Claude ────────────────────────────────────────────────────────────────────

def get_diagnosis(chat_id: int, user_content) -> str:
    _add(chat_id, "user", user_content)
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[WEB_SEARCH_TOOL],
        messages=_get_history(chat_id),
    )
    reply = _extract_text(resp.content)
    _add(chat_id, "assistant", reply)
    return reply

# ── Auth ──────────────────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    if ALLOWED_CHAT == 0:
        return True
    return update.effective_chat.id == ALLOWED_CHAT

# ── Handler ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "🔧 *OBD Diagnose Agent*\n\n"
        "Schick mir:\n"
        "• Hersteller + Modell + Fehlercode(s)\n"
        "  z.B. `BMW 3er G20 2021, P0300, U0100`\n"
        "• Screenshot deines OBD-Geräts oder der Diagnose-Software\n"
        "• Beides kombiniert\n\n"
        "Ich analysiere Fehler aus *OBD-II, CAN, FlexRay, Ethernet/DoIP, LIN* und suche "
        "online nach TSBs und bekannten Problemen.\n\n"
        "/reset — Neues Fahrzeug / Gespräch neu starten",
        parse_mode="Markdown",
    )

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    _history.pop(update.effective_chat.id, None)
    await update.message.reply_text("🔄 Konversation zurückgesetzt.")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = get_diagnosis(chat_id, update.message.text.strip())
        await update.message.reply_text(reply)
    except Exception as e:
        log.exception("Fehler bei Claude-Anfrage")
        await update.message.reply_text(f"⚠️ Fehler: {e}")

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        photo = update.message.photo[-1]
        tg_file = await ctx.bot.get_file(photo.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        caption = (
            update.message.caption
            or "Analysiere alle sichtbaren Fehlercodes, Werte und Statusinformationen auf diesem Screenshot."
        )
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": caption},
        ]
        reply = get_diagnosis(chat_id, content)
        await update.message.reply_text(reply)
    except Exception as e:
        log.exception("Fehler bei Bildverarbeitung")
        await update.message.reply_text(f"⚠️ Fehler beim Bild: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN nicht gesetzt")
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY nicht gesetzt")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    log.info("OBD Agent startet...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
