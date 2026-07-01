"""
Freie Datenquellen für den OBD-Agenten:
- Lokale SAE-DTC-Datenbank (dtc_codes.json)
- NHTSA API: Rückrufe + Kundenbeschwerden (kostenlos, kein Auth)
"""

import json
import urllib.request
from pathlib import Path

_DTC_DB: dict | None = None

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
