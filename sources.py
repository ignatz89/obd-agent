"""
Freie Datenquellen für den OBD-Agenten:
- Lokale SAE-DTC-Datenbank (dtc_codes.json)
- NHTSA API: Rückrufe + Kundenbeschwerden USA (kostenlos, kein Auth)
- KBA-Rückrufdatenbank: Rückrufe DE/EU (CSV-Export, kostenlos, kein Auth)
- NHTSA vPIC: VIN-Dekodierung (kostenlos, kein Auth)
"""

import csv
import json
import time
import urllib.request
from pathlib import Path
from typing import Optional

_DTC_DB: Optional[dict] = None

# ── KBA-Rückrufdatenbank (DE/EU) ────────────────────────────────────────────────
# Öffentlicher CSV-Export der Web-Suche unter kba-online.de/rrdb/buerger — kein API-Key,
# aber ~3.4 MB, deshalb lokal gecacht statt bei jeder Anfrage neu heruntergeladen.
KBA_CSV_URL       = "https://www.kba-online.de/rrdb/buerger/api/rueckruf/export?format=csv&type=cars"
KBA_CACHE_FILE    = Path(__file__).parent / "data" / "kba_rueckrufe.csv"
KBA_CACHE_MAX_AGE_DAYS = 30

def _load_dtc():
    global _DTC_DB
    if _DTC_DB is None:
        f = Path(__file__).parent / "dtc_codes.json"
        _DTC_DB = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    return _DTC_DB


def lookup_dtc(code: str) -> str:
    """SAE-Standard DTC-Code nachschlagen."""
    code = code.upper().strip()
    entry = _load_dtc().get(code)
    if entry:
        return f"{code}: {entry}"
    return f"{code}: Nicht in SAE-Standarddatenbank — vermutlich herstellerspezifisch (P1/B1/C1/U1xxx)."


def nhtsa_recalls(make: str, model: str, year: int) -> str:
    """NHTSA-Rückrufe für ein Fahrzeug abrufen."""
    try:
        url = f"https://api.nhtsa.gov/recalls/recallsByVehicle?make={make}&model={model}&modelYear={year}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        results = data.get("results", [])
        if not results:
            return f"Keine NHTSA-Rückrufe für {year} {make} {model}."
        lines = [f"NHTSA-Rückrufe für {year} {make} {model} ({len(results)} gesamt):"]
        for rec in results[:6]:
            lines.append(
                f"- [{rec.get('NHTSACampaignNumber', '?')}] "
                f"{rec.get('Component', '?')}: "
                f"{rec.get('Summary', '')[:250]}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"NHTSA-Rückruf-Abfrage fehlgeschlagen: {e}"


def nhtsa_complaints(make: str, model: str, year: int) -> str:
    """NHTSA-Kundenbeschwerden für ein Fahrzeug abrufen."""
    try:
        url = f"https://api.nhtsa.gov/complaints/complaintsByVehicle?make={make}&model={model}&modelYear={year}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        results = data.get("results", [])
        if not results:
            return f"Keine NHTSA-Beschwerden für {year} {make} {model}."
        by_comp: dict[str, int] = {}
        for rec in results:
            comp = rec.get("components", "Unbekannt")
            by_comp[comp] = by_comp.get(comp, 0) + 1
        lines = [f"NHTSA-Kundenbeschwerden für {year} {make} {model} ({len(results)} gesamt, nach Bauteil):"]
        for comp, count in sorted(by_comp.items(), key=lambda x: x[1], reverse=True)[:8]:
            lines.append(f"- {comp}: {count}x")
        return "\n".join(lines)
    except Exception as e:
        return f"NHTSA-Beschwerde-Abfrage fehlgeschlagen: {e}"


def decode_vin(vin: str) -> dict:
    """VIN per NHTSA vPIC dekodieren (funktioniert auch für EU-Fahrzeuge über die WMI-Herstellerkennung,
    Modell-/Motordetails können bei nicht in den USA verkauften Varianten aber unvollständig sein)."""
    vin = vin.strip().upper()
    try:
        url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        row = (data.get("Results") or [{}])[0]
        return {
            "make":           row.get("Make", ""),
            "model":          row.get("Model", ""),
            "year":           row.get("ModelYear", ""),
            "trim":           row.get("Trim", ""),
            "engine_model":   row.get("EngineModel", ""),
            "engine_cyl":     row.get("EngineCylinders", ""),
            "displacement_l": row.get("DisplacementL", ""),
            "fuel_type":      row.get("FuelTypePrimary", ""),
            "drive_type":     row.get("DriveType", ""),
            "body_class":     row.get("BodyClass", ""),
            "plant_country":  row.get("PlantCountry", ""),
            "error_text":     row.get("ErrorText", ""),
        }
    except Exception as e:
        return {"error": str(e)}


def _ensure_kba_cache() -> Path:
    """Lädt den KBA-CSV-Export herunter, falls er fehlt oder älter als KBA_CACHE_MAX_AGE_DAYS ist."""
    KBA_CACHE_FILE.parent.mkdir(exist_ok=True)
    if KBA_CACHE_FILE.exists():
        age_days = (time.time() - KBA_CACHE_FILE.stat().st_mtime) / 86400
        if age_days < KBA_CACHE_MAX_AGE_DAYS:
            return KBA_CACHE_FILE
    req = urllib.request.Request(KBA_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    KBA_CACHE_FILE.write_bytes(data)
    return KBA_CACHE_FILE


def kba_recalls(make: str, model: str, year: Optional[int] = None) -> str:
    """Deutsche/EU-Rückrufe (KBA-Rückrufdatenbank) für ein Fahrzeug abrufen."""
    try:
        csv_path = _ensure_kba_cache()
    except Exception as e:
        return f"KBA-Rückrufdatenbank nicht erreichbar: {e}"

    make_u  = make.strip().upper()
    model_u = model.strip().upper()

    matches = []
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                if make_u not in (row.get("Marke") or "").upper():
                    continue
                if model_u not in (row.get("Modell") or "").upper():
                    continue
                if year is not None:
                    von = (row.get("Produktionszeitraum von") or "").strip()
                    bis = (row.get("Produktionszeitraum bis") or "").strip()
                    try:
                        if von and year < int(von[:4]):
                            continue
                        if bis and year > int(bis[:4]):
                            continue
                    except ValueError:
                        pass
                matches.append(row)
    except Exception as e:
        return f"KBA-CSV konnte nicht gelesen werden: {e}"

    if not matches:
        suffix = f" ({year})" if year else ""
        return f"Keine KBA-Rückrufe für {make} {model}{suffix} gefunden."

    matches.sort(key=lambda r: r.get("Veröffentlichungsdatum") or "", reverse=True)

    suffix = f" ({year})" if year else ""
    lines = [f"KBA-Rückrufe (DE/EU) für {make} {model}{suffix} ({len(matches)} gesamt):"]
    for rec in matches[:6]:
        lines.append(
            f"- [{rec.get('KBA-Referenznummer', '?')}] {rec.get('Veröffentlichungsdatum', '?')}: "
            f"{rec.get('Mangelbezeichnung', '')[:200]}"
            + (f" | Maßnahme: {rec['Beschreibung der Maßnahme'][:150]}"
               if rec.get("Beschreibung der Maßnahme") not in (None, "", "N/A") else "")
        )
    return "\n".join(lines)
