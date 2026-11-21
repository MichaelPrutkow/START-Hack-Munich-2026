# Entscheidungen und Messwerte (Basis fürs Write-up)

Eine Zeile pro Änderung an Strategie oder Parametern: was, warum (Messung, Quelle), ab welchem Spiel aktiv, was danach gemessen wurde. Neueste unten. Keine Keys, keine Falldaten.

Team auf dem Leaderboard: `chineseModels`. Runner: nur Philips Mac.

| Zeit (21.11.) | Entscheidung | Beleg | Aktiv ab | Ergebnis danach |
|---|---|---|---|---|
| 13:46 | Runner gegen echtes `API_HANDBOOK.md` geprüft, Spiel 0 durchgespielt | Echo ok, 4/4 Positionen, fertig nach 10 s | Spiel 1 | Spiel 1: +1.601 (Rang 4 von 12) |
| 14:05 | `a = 0,9 × m` statt `a = m`; `b` = unteres Drittel der t-Schätzung (`p·P(T ≥ b) = 2/3`, `SIGMA 0,35`) statt `b = a` | Auszahlungssimulation nach offiziellen Regeln: schlägt `a = b = m` in 8 von 9 Szenarien (Unsicherheit 20–50 %, Gegner vorsichtig/naiv/großzügig) | Spiel 2 (aus unseren a-Werten erschlossen: 342 = 0,9·380, 85,5 = 0,9·95) | Spiel 2: −342; b fast perfekt, a in 2 von 3 gedeckten Positionen knapp über t |
| 14:05 | Parser: letzte Zahl+Einheit der Zeile, umbrochene Beschreibungen anhängen | Case-0-PDF: 4/4 Positionen, vollständige Texte (`test_runner.py`) | Spiel 2 | – |
| 14:11 | Leaderboard-Transaktionen zur Kalibrierung nutzen (`calibrate.py`, 1 Anfrage/s, Cache) | Orga erlaubt Scrapen, nur nicht im ms-Takt (mündlich an Michael) | – | t-Spannen je Position aus Spiel 1–2 |
| 14:20 | Empfehlung `A_MULT = 0,85` (per `.env` auf Philips Mac) | Backtest gegen echte a/b aller Teams, 4 Positionen aus Spiel 1–2: 0,7 → 9.974 · 0,8 → 12.232 · **0,85 → 13.551** · 0,9 → 9.753 · 1,0 → 2.618. In 3 von 4 gedeckten Positionen lag unser a knapp über t | Spiel 3 (erschlossen: 246,50 / 157,25 / 55,25 = 0,85 × 290 / 185 / 65) | Spiel 3: +4.528, bestes Team der Runde; beide gedeckten Positionen fair. Lecks: Pos. 4 b zu niedrig (2 faire Forderungen abgelehnt, 877 statt 585 gezahlt), Pos. 6 drei überhöhte angenommen (116) |

## Offen

- Nach 5–10 weiteren Spielen `uv run calibrate.py --team chineseModels` auf Philips Mac: t/m-Verteilung → `A_MULT`, `SIGMA` neu setzen und hier eintragen.
