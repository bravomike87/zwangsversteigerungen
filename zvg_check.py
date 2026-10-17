#!/usr/bin/env python3
"""Taegliche Pruefung des ZVG-Portals auf passende Zwangsversteigerungen.

Sucht Termine der Amtsgerichte rund um Bonn, filtert auf Objekte in Bonn/Wachtberg
mit mind. MIN_ZIMMER Zimmern und MIN_QM Wohnflaeche und verschickt eine Mail.
Nur Python-Standardbibliothek.

Aufruf:  python zvg_check.py [--dry-run]
Mail ueber Umgebungsvariablen SMTP_USER, SMTP_PASSWORD, MAIL_TO (kommagetrennt).
"""
import html
import json
import os
import re
import smtplib
import sys
import subprocess
import tempfile
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

BASE = "https://www.zvg-portal.de/index.php"
STATE_FILE = Path(__file__).parent / "state.json"

# (Name, ger_id): Wachtberg gehoert je nach Quelle zu Bonn oder Rheinbach, daher mehrere Gerichte.
COURTS = [("Bonn", "R3201"), ("Rheinbach", "R3207"), ("Siegburg", "R3208"), ("Königswinter", "R3205")]
ORTE = ("bonn", "wachtberg")
MIN_ZIMMER = 4
MIN_QM = 100
# Bei Haeusern mit ausreichender Wohnflaeche wird die Zimmerzahl als erfuellt angenommen,
# wenn das Portal keine Zimmerzahl nennt. Auf False setzen fuer strikte Pruefung.
HAUS_IMPLIZIERT_ZIMMER = True
HAUS_TYPEN = ("haus", "villa", "bungalow", "doppelhaushälfte", "reihenhaus")

ZAHLWORTE = {"vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10}

COOKIES = tempfile.NamedTemporaryFile(prefix="zvg_cookies_", delete=False).name


def fetch(url, data=None):
    """Abruf per curl (nutzt den System-Zertifikatsspeicher, das Portal liefert keine volle Kette)."""
    cmd = ["curl", "-sS", "--fail", "-L", "--max-time", "60", "-b", COOKIES, "-c", COOKIES,
           "-A", "Mozilla/5.0 (zvg-check, privat)", "-e", f"{BASE}?button=Suchen", url]
    for k, v in (data or {}).items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    return subprocess.run(cmd, check=True, capture_output=True).stdout.decode("utf-8", errors="replace")


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def search_ids(name, ger_id):
    fetch(f"{BASE}?button=Termine%20suchen")  # Session-Cookie holen
    page = fetch(f"{BASE}?button=Suchen", {
        "ger_name": name, "order_by": "2", "land_abk": "nw", "ger_id": ger_id,
        "az1": "", "az2": "", "az3": "", "az4": "", "art": "", "obj": "", "str": "",
        "hnr": "", "plz": "", "ort": "", "ortsteil": "", "vtermin": "", "btermin": "",
    })
    return list(dict.fromkeys(re.findall(r"button=showZvg&zvg_id=(\d+)&land_abk=nw", page)))


def parse_detail(zvg_id):
    t = text_of(fetch(f"{BASE}?button=showZvg&zvg_id={zvg_id}&land_abk=nw"))
    def grab(pattern):
        m = re.search(pattern, t)
        return m.group(1).strip() if m else ""
    return {
        "id": zvg_id,
        "az": grab(r"^.*?(\d{4} K \d{4}/ ?\d{4})").replace("/ ", "/"),
        "updated": grab(r"letzte Aktualisierung: ([\d\- :]+)\)"),
        "objekt": grab(r"Objekt/Lage: (.*?)(?: Beschreibung:| Verkehrswert in)"),
        "beschreibung": grab(r"Beschreibung: (.*?) Verkehrswert in"),
        "wert": grab(r"Verkehrswert in [^:]*: ([\d.,]+)"),
        "termin": grab(r"Termin: (.*?) Ort der Versteigerung"),
        "gutachten": "Gutachten:" in t,
    }


def wohnflaeche(txt):
    zahlen = []
    for m in re.finditer(r"Wohn(?:/Nutz)?fl[äa]che[^.\d]{0,60}?(\d+(?:[.,]\d+)?)\s*(?:m²|m2|qm|m ²)", txt, re.I):
        zahlen.append(m.group(1))
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|qm|m ²)\s*Wohnfl", txt, re.I):
        zahlen.append(m.group(1))
    werte = [float(z.replace(".", "").replace(",", ".")) if "," in z else float(z) for z in zahlen]
    return max(werte) if werte else None


def zimmer(txt):
    werte = [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:[.,]5)?)[- ]?(?:Zimmer|Zi\.|Räume)", txt)]
    for wort, n in ZAHLWORTE.items():
        if re.search(rf"\b{wort}[- ]?(?:Zimmer|Räume)", txt, re.I):
            werte.append(n)
    return max(werte) if werte else None


def datum_ok(termin):
    monate = ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august",
              "september", "oktober", "november", "dezember"]
    m = re.search(r"(\d{1,2})\. (\w+) (\d{4})", termin)
    if not m or m.group(2).lower() not in monate:
        return True  # im Zweifel anzeigen
    d = date(int(m.group(3)), monate.index(m.group(2).lower()) + 1, int(m.group(1)))
    return d >= date.today()


def bewerte(o):
    txt = f"{o['objekt']} {o['beschreibung']}"
    if not any(ort in o["objekt"].lower() for ort in ORTE):
        return None
    if not datum_ok(o["termin"]):
        return None
    qm, zi = wohnflaeche(txt), zimmer(txt)
    if qm is None or qm < MIN_QM:
        return None
    angenommen = False
    if zi is None and HAUS_IMPLIZIERT_ZIMMER and any(h in o["objekt"].lower() for h in HAUS_TYPEN):
        zi, angenommen = MIN_ZIMMER, True
    if zi is None or zi < MIN_ZIMMER:
        return None
    o.update(qm=qm, zimmer=zi, zimmer_angenommen=angenommen)
    return o


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def render(neu, geaendert, bestand, entfallen):
    def zeile(o, tag=""):
        tage = o["seit"]
        alter = "heute erstmals gelistet" if tage == 0 else f"seit {tage} {'Tag' if tage == 1 else 'Tagen'} gelistet"
        zi = f"{o['zimmer']:g} Zi." + (" (angenommen, Haus)" if o["zimmer_angenommen"] else "")
        return (f"{tag}{o['objekt']}  ({alter})\n  {o['qm']:g} m² Wohnfläche, {zi}, Verkehrswert {o['wert']} €\n"
                f"  Termin: {o['termin']}\n  Az. {o['az']} (AG {o['gericht']}), aktualisiert {o['updated']}\n"
                f"  {o['beschreibung'][:300]}\n")
    teile = []
    for titel, liste, tag in [("NEU", neu, "[NEU] "), ("GEÄNDERT (seit der letzten Mail)", geaendert, "[GEÄNDERT] "),
                              ("Weiterhin im Verfahren", bestand, "")]:
        if liste:
            teile.append(f"== {titel} ==\n" + "\n".join(zeile(o, tag) for o in liste))
    if entfallen:
        teile.append("== Nicht mehr gelistet (aufgehoben/versteigert) ==\n" +
                     "\n".join(f"{e['objekt']} (Az. {e['az']})" for e in entfallen))
    if not teile:
        teile.append("Heute keine passenden Objekte (Bonn/Wachtberg, ab 4 Zimmer und 100 m²).")
    teile.append("Quelle: https://www.zvg-portal.de (Termine suchen, Amtsgericht Bonn). "
                 "Angaben aus dem Portaltext, vor einem Gebot immer das Gutachten prüfen.")
    return "\n\n".join(teile)


def send_mail(subject, body):
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def main():
    dry = "--dry-run" in sys.argv
    treffer = {}
    for name, ger_id in COURTS:
        for zvg_id in search_ids(name, ger_id):
            if zvg_id in treffer:
                continue
            o = bewerte(parse_detail(zvg_id) | {"gericht": name})
            if o:
                treffer[zvg_id] = o
    state = load_state()
    heute = date.today()
    for i, o in treffer.items():
        erst = date.fromisoformat(state.get(i, {}).get("first_seen", heute.isoformat()))
        o["first_seen"], o["seit"] = erst.isoformat(), (heute - erst).days
    neu = [o for i, o in treffer.items() if i not in state]
    geaendert = [o for i, o in treffer.items() if i in state and state[i]["updated"] != o["updated"]]
    bestand = [o for i, o in treffer.items() if i in state and state[i]["updated"] == o["updated"]]
    entfallen = [v for i, v in state.items() if i not in treffer]
    body = render(neu, geaendert, bestand, entfallen)
    subject = f"Zwangsversteigerungen Bonn/Wachtberg {date.today():%d.%m.%Y}: " + (
        f"{len(neu)} neu, {len(treffer)} gesamt" if treffer else "keine Treffer")
    print(subject, "\n", body, sep="\n")
    if dry:
        return
    send_mail(subject, body)
    STATE_FILE.write_text(json.dumps(
        {i: {"updated": o["updated"], "az": o["az"], "objekt": o["objekt"], "first_seen": o["first_seen"]} for i, o in treffer.items()},
        indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
