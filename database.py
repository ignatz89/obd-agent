"""
Persistente Diagnose-Datenbank für den KFZ-Agenten.

Speichert jede abgeschlossene Diagnosesession als JSON-Eintrag in
data/diagnoses.json. Beim /reset-Befehl wird die aktuelle Konversation
per Claude Haiku in strukturierte Felder extrahiert und gespeichert.

Schema pro Eintrag:
  id            — Timestamp-basierte ID (z.B. 20260703-143022)
  date          — Datum/Uhrzeit der Diagnose
  make          — Fahrzeughersteller
  model         — Modell + Generation
  year          — Baujahr (int oder null)
  dtc_codes     — Liste der gefundenen DTC-Codes
  symptoms      — Kurzbeschreibung der Symptome
  summary       — 1-2 Satz Diagnose-Zusammenfassung
  raw_messages  — vollständige Konversation (nur User+Assistent-Text)
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic

DATA_FILE = Path(__file__).parent / "data" / "diagnoses.json"
_EXTRACT_MODEL = "claude-haiku-4-5"


# ── Laden / Speichern ──────────────────────────────────────────────────────────

def _load() -> list:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save(entries: list) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── DTC-Codes per Regex vorextrahieren ────────────────────────────────────────

def _find_dtc_codes(text: str) -> list[str]:
    return sorted(set(re.findall(r'\b[PBCU][0-9]{4}\b', text.upper())))


# ── Haiku-Extraktion ───────────────────────────────────────────────────────────

def _extract_with_haiku(client: anthropic.Anthropic, conversation_text: str) -> dict:
    """Extrahiert strukturierte Felder aus dem Gesprächstext via Haiku."""
    prompt = (
        "Extrahiere aus diesem Fahrzeug-Diagnosegespräch die folgenden Felder als JSON.\n"
        "Falls ein Feld nicht erkennbar ist, setze null.\n\n"
        "Gewünschtes Format:\n"
        "{\n"
        '  "make": "Hersteller (z.B. BMW, VW, Mercedes)",\n'
        '  "model": "Modell + Generation (z.B. 3er G20, Golf 8, C-Klasse W206)",\n'
        '  "year": Baujahr als Zahl oder null,\n'
        '  "dtc_codes": ["Liste", "der", "DTC-Codes"],\n'
        '  "symptoms": "Kurze Symptom-Beschreibung in 1-2 Sätzen",\n'
        '  "summary": "Diagnose-Zusammenfassung in 1-2 Sätzen"\n'
        "}\n\n"
        "Gespräch:\n"
        + conversation_text[:4000]
    )
    try:
        resp = client.messages.create(
            model=_EXTRACT_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return {}


# ── Öffentliche API ────────────────────────────────────────────────────────────

def save_session(history: list, client: anthropic.Anthropic) -> Optional[dict]:
    """
    Extrahiert und speichert eine Diagnosesession.
    Erwartet die Konversations-History aus _history[chat_id].
    Gibt den gespeicherten Eintrag zurück oder None bei leerer History.
    """
    # Nur Text-Nachrichten für Extraktion verwenden
    user_texts = []
    assistant_text = ""
    simple_messages = []

    for msg in history:
        role    = msg.get("role", "")
        content = msg.get("content", "")

        # content kann str oder list sein (bei Tool-Results)
        if isinstance(content, str) and content.strip():
            simple_messages.append({"role": role, "content": content})
            if role == "user":
                user_texts.append(content)
            elif role == "assistant":
                assistant_text = content  # letzter Assistent-Text

    if not simple_messages:
        return None  # Nichts zu speichern

    conversation_text = "\n\n".join(
        f"[{m['role'].upper()}]: {m['content']}" for m in simple_messages
    )

    # DTC-Codes per Regex aus User-Eingaben vorab extrahieren
    all_user_text = " ".join(user_texts)
    dtc_codes_regex = _find_dtc_codes(all_user_text)

    # Strukturierte Extraktion per Haiku
    extracted = _extract_with_haiku(client, conversation_text)

    # Codes aus Haiku-Ergebnis mit Regex-Ergebnis zusammenführen
    haiku_codes = extracted.get("dtc_codes") or []
    all_codes   = sorted(set(dtc_codes_regex + [c.upper() for c in haiku_codes if isinstance(c, str)]))

    entry = {
        "id":           datetime.now().strftime("%Y%m%d-%H%M%S"),
        "date":         datetime.now().strftime("%Y-%m-%d %H:%M"),
        "make":         extracted.get("make")     or "",
        "model":        extracted.get("model")    or "",
        "year":         extracted.get("year")     or None,
        "dtc_codes":    all_codes,
        "symptoms":     extracted.get("symptoms") or all_user_text[:300],
        "summary":      extracted.get("summary")  or assistant_text[:300],
        "raw_messages": simple_messages,
    }

    entries = _load()
    entries.append(entry)
    _save(entries)
    return entry


def load_all() -> list:
    """Alle gespeicherten Diagnosen laden (neueste zuerst)."""
    return list(reversed(_load()))


def search(query: str) -> list:
    """
    Suche in Diagnosen nach Hersteller, Modell, DTC-Code oder Symptom.
    Gibt passende Einträge zurück (neueste zuerst).
    """
    q = query.lower().strip()
    results = []
    for entry in _load():
        searchable = " ".join([
            entry.get("make", ""),
            entry.get("model", ""),
            " ".join(entry.get("dtc_codes", [])),
            entry.get("symptoms", ""),
            entry.get("summary", ""),
        ]).lower()
        if q in searchable:
            results.append(entry)
    return list(reversed(results))


def format_entry(entry: dict, show_summary: bool = True) -> str:
    """Formatiert einen Eintrag für die Telegram-Ausgabe."""
    make   = entry.get("make")  or "?"
    model  = entry.get("model") or "?"
    year   = entry.get("year")
    codes  = ", ".join(entry.get("dtc_codes") or []) or "–"
    date   = entry.get("date", "?")
    sympt  = entry.get("symptoms", "")[:200]
    summ   = entry.get("summary", "")[:300]

    vehicle = f"{make} {model}" + (f" ({year})" if year else "")
    lines = [
        f"🚗 *{vehicle}*",
        f"📅 {date}",
        f"🔴 Codes: `{codes}`",
    ]
    if sympt:
        lines.append(f"📋 Symptome: {sympt}")
    if show_summary and summ:
        lines.append(f"🔧 Diagnose: {summ}")
    return "\n".join(lines)
