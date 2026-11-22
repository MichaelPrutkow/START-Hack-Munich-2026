# Transaktionsanalyse Spiele 1–9 (Stand 21.11., 16:10)

Quelle: öffentliche Leaderboard-Transaktionen, Cache `out/leaderboard/game_001–009.json` auf Michaels Mac (alle 12 Teams vollständig, keine neue Anfrage an die Plattform). Skripte: `starthack/analysen/skripte/` (laufen im Ordner mit den `game_*.json`).

## Stand nach 9 Spielen

| Rang | Team | Summe | G7–9 |
|---|---|---|---|
| 1 | 404 | 43.289 | +13.009 |
| 2 | alpha | 17.582 | −5.109 |
| 3 | FDN | 14.664 | +12.484 |
| 4 | TRINNERS4WINNERS | 14.604 | +8.999 |
| 5 | **chineseModels** | **9.624** | **−3.493** |

## Unsere Verlusttreiber pro Spiel

| Spiel | Netto (Rang) | Haupttreiber |
|---|---|---|
| G1 | +1.601 (4) | Pos. 1: a = 900 knapp über t ∈ [781; 900). 404 forderte 781 (fair) und holte +11.137 |
| G2 | −342 (8) | Pos. 1: a über t (≥ 808), überall abgelehnt; als Reviewer 4.266 gezahlt. FDN forderte 808 fair (+13.296) |
| G3 | +4.528 (1) | – |
| G4 | −1.299 (7) | Pos. 1–2 (geteiltes Limit): a unfair und b nahm unfaire an (Überzahlung 1.595) |
| G5 | +7.859 (3) | – |
| G6 | +770 (6) | Pos. 1: m = 550 bei t ≥ 744, a zu niedrig. 404 forderte 744 (+8.140) |
| G7 | −326 (7) | Pos. 1: t ≈ 450, unser m ≈ 800. a = 680 unfair, b ≈ 700 nahm 5 unfaire Forderungen an (Überzahlung 2.597) |
| G8 | −703 (10) | Entsorgungsfall: t auf Pos. 1, 3, 4 nur ⅓ unseres m. Alle Forderungen unfair, b nahm unfaire an (2.151) |
| G9 | −2.464 (9) | Pos. 1: t < 336 bei m ≈ 1000; Pos. 4: t ∈ [517; 616) bei m ≈ 1000. b ≈ 860 nahm 12 unfaire Forderungen an (Überzahlung 8.946) |

**Kern:** In G7–9 schätzen LLMs (wir und das Feld) t um Faktor 1,6–5 zu hoch. Unser `b ≈ 1,0 × a` (SIGMA 0,35) nimmt dann fast jede Forderung des Feldes an. Reviewer-Zahlungen G7–9: wir 15.495, 404 1.755. Das sind ~80 % unseres Rückstands in G7–9. Issuer-Einnahmen G7–9: wir 12.002, 404 14.765, FDN 15.913.

## Was die Top-Teams anders machen

Reviewer-Verhalten (Forderungen der anderen mit a > 0):

| Team | G1–6: unfaire angenommen | G1–6: faire abgelehnt | G7–9: unfaire angenommen | gezahlt für unfaire G7–9 |
|---|---|---|---|---|
| 404 | 20 % | 61 % | **4 %** | 223 |
| FDN | 70 % | 43 % | **24 %** | 2.098 |
| TRINNERS | 0 % | 77 % | 36 % | 3.380 |
| alpha | 39 % | 22 % | 80 % | 14.973 |
| chineseModels | 50 % | 16 % | 59 % | 13.665 |

- **404 ist durchgehend ein strenger Reviewer.** b liegt meist deutlich unter dem eigenen a (b/a oft 0,6–0,7, nicht konstant). Sie nehmen bewusst Strafen für abgelehnte faire Forderungen in Kauf (G1–6: 6.437), weil eine angenommene unfaire Forderung den vollen Betrag kostet, eine abgelehnte faire nur 0,5 a extra.
- **404 als Issuer:** viermal die höchste noch faire Forderung auf der größten Position (G1, G4 Pos. 3, G6, G9 Pos. 4 fast). In G7–9 lagen ihre a bei 0,4–0,9 × unseren. Kein fester Faktor: ihre Schätzung ist auf großen Positionen schlicht genauer (in G6 lagen sie 1,6 × über dem Feld und fair).
- **FDN hat ab G7 umgestellt** (unfaire angenommen 70 % → 24 %) und ist damit in G7–9 auf +12.484 gekommen. Forderungen niedrig (G7 458 bei t 449, G9 Pos. 4 517 = höchste faire).
- **alpha** hat dasselbe Problem wie wir (großzügiges b) und ist in G7–9 von Platz 2 abgerutscht.

## Vorschläge, priorisiert

Backtest: echte Transaktionen aller 9 Spiele, andere Teams fest, unser m = a / A_MULT aus dem Live-Spiel (G2 Pos. 1 und G4 Pos. 2 geschätzt, dort ist unser a unbekannt). Werte = unser Netto über alle 9 Spiele mit A_MULT 0,85.

| Variante | b / m | G1–6 | G7–9 | Summe | Δ |
|---|---|---|---|---|---|
| SIGMA 0,35 (jetzt) | 0,85 | 29.053 | −2.636 | 26.417 | – |
| SIGMA 0,65 | 0,75 | 27.582 | 3.376 | 30.958 | +4.541 |
| **SIGMA 1,0** | **0,64** | 26.242 | 5.650 | **31.892** | **+5.475** |
| SIGMA 1,2 | 0,58 | 25.328 | 7.344 | 32.672 | +6.255 |

1. **SIGMA 0,35 → 1,0** (nur `.env`, Neustart zwischen zwei Spielen). b sinkt von ≈ 1,0 × a auf ≈ 0,75 × a. Begründung zusätzlich zum Backtest: zensierte Schätzung von log(t/m) über unsere 19 Zeilen ergibt log-sd 0,65 (nur sicher gedeckte Zeilen) bis 1,0 (inkl. möglicher Deckungsfehler). 0,35 ist zu eng. Kosten: in Fällen wie G5/G6 (t über unserem m) lehnen wir mehr faire Forderungen ab (−2.811 in G1–6).
2. **„Lean high“ bei uneinigen Stimmen nicht fürs b verwenden.** `DISAGREE` setzt m auf das Mittel der zwei höchsten Stimmen und hebt damit auch b. Unsicherheit sollte b senken. Vorschlag: b aus dem Median (oder der niedrigsten) Stimme. Backtest braucht die Stimmen-Logs aus `runner.log` auf Philips Mac.
3. **A_MULT bei 0,85 lassen.** 0,8 sieht im Backtest besser aus, aber fast nur wegen G2 Pos. 1, wo unser m geraten ist. Ohne diese Zeile neutral.
4. **Größter Hebel auf der Issuer-Seite bleibt m auf der größten Position.** In G7–9 überschätzen LLMs Handwerks- und Entsorgungspreise um Faktor 2–3. Recall der eigenen Klammern aus G1–9 als Kalibrierbeispiele ins Prompt (Fälle 1–8 liegen lokal), Test mit `eval_cases.py`.

Grenzen: Backtest hält die anderen Teams fest (wenn mehr Teams streng werden, verdienen unfaire Forderungen noch weniger, was für niedrige a spricht). Offene b-Intervalle sind linear interpoliert. G9 liegt lokal nicht entschlüsselt vor, Ursache für t < 336 auf Pos. 1 (Preis oder Deckung) ist ungeprüft.
