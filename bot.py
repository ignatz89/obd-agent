"""
OBD Diagnostic Agent — Telegram Bot

Analysiert Fahrzeugfehler (OBD, CAN, FlexRay, Ethernet/DoIP, LIN) per:
- Text: Hersteller + Modell + Fehlercode(s)
- Screenshot: OBD-Auslesegerät, Diagnose-Software

Datenquellen:
- Lokale SAE-DTC-Datenbank (dtc_codes.json)
- NHTSA API: US-Rückrufe + Kundenbeschwerden (kostenlos)
- Claude Web Search: TSBs, Foren, herstellerspezifische Codes

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

from sources import lookup_dtc, nhtsa_recalls, nhtsa_complaints
from database import save_session, load_all, search, format_entry

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

# ── Tools ─────────────────────────────────────────────────────────────────────
TOOLS = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    {
        "name": "lookup_dtc",
        "description": "SAE-Standard Fehlercode (DTC) in lokaler Datenbank nachschlagen. Gibt offizielle Bezeichnung zurück. Immer zuerst aufrufen bevor online gesucht wird.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "DTC-Code z.B. P0300, U0100, B1342, C0035"}
            },
            "required": ["code"],
        },
    },
    {
        "name": "nhtsa_recalls",
        "description": "NHTSA-Rückrufe (USA) für ein Fahrzeug abrufen. Enthält betroffene Bauteile und Beschreibung.",
        "input_schema": {
            "type": "object",
            "properties": {
                "make":  {"type": "string",  "description": "Hersteller auf Englisch z.B. BMW, Toyota, Volkswagen"},
                "model": {"type": "string",  "description": "Modell auf Englisch z.B. '3 Series', 'Corolla', 'Golf'"},
                "year":  {"type": "integer", "description": "Baujahr z.B. 2021"},
            },
            "required": ["make", "model", "year"],
        },
    },
    {
        "name": "nhtsa_complaints",
        "description": "NHTSA-Kundenbeschwerden (USA) für ein Fahrzeug abrufen. Zeigt häufigste Problembereiche nach Anzahl.",
        "input_schema": {
            "type": "object",
            "properties": {
                "make":  {"type": "string",  "description": "Hersteller auf Englisch"},
                "model": {"type": "string",  "description": "Modell auf Englisch"},
                "year":  {"type": "integer", "description": "Baujahr"},
            },
            "required": ["make", "model", "year"],
        },
    },
]

# ── Konversations-History (pro Chat, in-memory) ───────────────────────────────
_history: dict[int, list] = {}
CONTEXT_MESSAGES = 20

def _get_history(chat_id: int) -> list:
    return _history.setdefault(chat_id, [])

def _trim(chat_id: int) -> None:
    h = _history.get(chat_id, [])
    if len(h) > CONTEXT_MESSAGES:
        _history[chat_id] = h[-CONTEXT_MESSAGES:]

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.text for b in content if hasattr(b, "text") and b.text).strip()

# ── Tool-Ausführung ────────────────────────────────────────────────────────────

def _run_tool(name: str, inputs: dict) -> str:
    log.info(f"Tool: {name}({inputs})")
    if name == "lookup_dtc":
        return lookup_dtc(inputs["code"])
    if name == "nhtsa_recalls":
        return nhtsa_recalls(inputs["make"], inputs["model"], int(inputs["year"]))
    if name == "nhtsa_complaints":
        return nhtsa_complaints(inputs["make"], inputs["model"], int(inputs["year"]))
    return f"Unbekanntes Tool: {name}"

# ── Claude mit Tool-Loop ───────────────────────────────────────────────────────

def get_diagnosis(chat_id: int, user_content) -> str:
    h = _get_history(chat_id)
    h.append({"role": "user", "content": user_content})

    messages = list(h)

    for _ in range(15):  # max 15 Tool-Runden
        resp = claude.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            thinking={"type": "disabled"},   # wie bisher ohne Thinking — Sonnet 5 hätte es sonst standardmäßig an
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            reply = _extract_text(resp.content)
            h.append({"role": "assistant", "content": reply})
            _trim(chat_id)
            return reply

        # Tool-Calls ausführen
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = _run_tool(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": results})

    return "⚠️ Analyse konnte nicht abgeschlossen werden (zu viele Tool-Runden)."

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
        "🔧 *KFZ Diagnose Agent*\n\n"
        "Schick mir Fahrzeug + Fehlercode(s):\n"
        "`BMW 3er G20 2021, P0300, U0100`\n\n"
        "Oder einen *Screenshot* deines OBD-Geräts.\n\n"
        "Ich analysiere Fehler aus OBD-II, CAN, FlexRay, Ethernet/DoIP, LIN und suche:\n"
        "• SAE-Standarddatenbank (lokal)\n"
        "• NHTSA Rückrufe & Kundenbeschwerden\n"
        "• Web (TSBs, Foren, herstellerspezifische Codes)\n\n"
        "/reset — Neues Fahrzeug (speichert aktuelle Diagnose)\n"
        "/history — Letzte Diagnosen anzeigen\n"
        "/suche BMW — Diagnosen nach Fahrzeug oder Code suchen",
        parse_mode="Markdown",
    )

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    history = _history.pop(chat_id, [])

    if history:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            entry = save_session(history, claude)
            if entry:
                vehicle = f"{entry.get('make', '')} {entry.get('model', '')}".strip() or "Fahrzeug"
                codes   = ", ".join(entry.get("dtc_codes") or [])
                saved_msg = f"💾 Diagnose gespeichert: *{vehicle}*" + (f" — `{codes}`" if codes else "")
            else:
                saved_msg = "💾 Diagnose gespeichert."
        except Exception as e:
            log.exception("Fehler beim Speichern der Diagnose")
            saved_msg = f"⚠️ Speichern fehlgeschlagen: {e}"
        await update.message.reply_text(saved_msg, parse_mode="Markdown")

    await update.message.reply_text("🔄 Zurückgesetzt — neues Fahrzeug?")


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Letzte Diagnosen anzeigen."""
    if not is_allowed(update):
        return
    entries = load_all()[:5]
    if not entries:
        await update.message.reply_text("📂 Noch keine Diagnosen gespeichert.")
        return
    parts = [f"📂 *Letzte {len(entries)} Diagnosen:*\n"]
    for e in entries:
        parts.append(format_entry(e, show_summary=False))
    await update.message.reply_text("\n\n".join(parts), parse_mode="Markdown")


async def cmd_suche(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnosen durchsuchen: /suche BMW  oder  /suche P0300"""
    if not is_allowed(update):
        return
    query = update.message.text.removeprefix("/suche").strip()
    if not query:
        await update.message.reply_text("Verwendung: `/suche BMW` oder `/suche P0300`", parse_mode="Markdown")
        return
    results = search(query)[:5]
    if not results:
        await update.message.reply_text(f"🔍 Keine Diagnosen gefunden für: *{query}*", parse_mode="Markdown")
        return
    parts = [f"🔍 *{len(results)} Treffer für \"{query}\":*\n"]
    for e in results:
        parts.append(format_entry(e, show_summary=True))
    await update.message.reply_text("\n\n".join(parts), parse_mode="Markdown")

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
            or "Analysiere alle sichtbaren Fehlercodes, Messwerte und Statusinformationen."
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
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("suche",   cmd_suche))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    log.info("OBD Agent startet...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
