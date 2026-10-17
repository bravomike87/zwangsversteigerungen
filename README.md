# Zwangsversteigerungen Bonn/Wachtberg

`zvg_check.py` fragt täglich das ZVG-Portal (zvg-portal.de) ab, filtert auf Bonn/Wachtberg,
mind. 4 Zimmer und 100 m² Wohnfläche und mailt das Ergebnis. `state.json` merkt sich, was
schon gemeldet wurde (neu / geändert / nicht mehr gelistet).

Test lokal ohne Mailversand: `python3 zvg_check.py --dry-run`

Einrichtung GitHub: Repo anlegen, pushen, unter Settings > Secrets > Actions die Secrets
`SMTP_USER` (Gmail-Adresse des Absenders) und `SMTP_PASSWORD` (Gmail-App-Passwort) setzen.
Filter (Orte, Zimmer, m²) stehen oben in `zvg_check.py`.
