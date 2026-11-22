# Warum führt Team 404? (Stand 21.11. 14:35, Spiele 1–3)

Quelle: öffentliche Leaderboard-Transaktionen aus `out/leaderboard/game_00{1,2,3}.json` (alle 12 Teams vollständig, keine neue Anfrage). t-Spanne je Position wie in `calibrate.py`: `[höchste faire Forderung, niedrigste unfaire Forderung)`. Nur 6 gedeckte Positionen, also dünne Datenlage.

## Zahlen

| Team | G1 | G2 | G3 | Summe | Issuer + | Reviewer − |
|---|---|---|---|---|---|---|
| 404 | 11.137 (1.) | 207 (7.) | 4.265 (2.) | **15.609** | 27.067 | 11.459 |
| alpha | 2.182 | 6.792 | 3.432 | 12.406 | 23.181 | 10.776 |
| TRINNERS4WINNERS | 5.024 | 3.512 | 2.121 | 10.657 | | |
| FDN | −8.022 | 13.296 | 1.662 | 6.936 | | |
| chineseModels | 1.601 | −342 | 4.528 (1.) | 5.787 | 15.248 | 9.463 |

## Befunde

1. **Der Vorsprung kommt aus einer einzigen Position.** G1 Pos. 1: t in [780,69; 900). 404 forderte 780,69 (die höchste faire Forderung), wir 900 (knapp unfair, damals `a = m`). Netto auf dieser Zeile 404 +10.752, wir +1.351. Das sind 9.401 der 9.822 Rückstand (96 %). In G2+G3 zusammen: 404 4.472, wir 4.186.
2. **Zweitgrößter Posten: G2 Pos. 1** (t ≥ 808,45): unser a lag darüber (0 Einnahmen, −4.266 Reviewer-Kosten), 404 forderte konservativ 443 (+31 netto), FDN 808,45 (+9.478). Dagegen gewannen wir G2 Pos. 2 (+3.573 vor 404).
3. **Muster jedes Spiels:** Wer auf der größten gedeckten Position die höchste noch faire Forderung stellt, gewinnt die Runde (G1 404, G2 FDN, G3 alpha Pos. 4 bzw. wir Pos. 5). Grund: 5–6 Teams lehnen alles ab (a = b = 0), dazu niedrige b. Eine faire Forderung bringt daher etwa 14–16 × a, eine knapp unfaire nur 1–3 × a (nur die Annehmenden zahlen). Überschießen kostet 80–90 % der Zeile.
4. **404 ist kein konstanter Faktor unter dem Median.** a(404)/Median der positiven Forderungen auf gedeckten Zeilen: 0,93 / 0,71 / 0,84 / 1,40 / 1,20 / 1,14. a(404)/a(wir): 0,87 / 0,65 / 0,98 / 1,04 / 0,97 (G2 Pos. 1: < 0,55). In G3 liegen 404 und wir fast gleich (auch identisches b-Intervall auf Pos. 6). Vermutung: ähnlicher LLM-Ansatz; Unterschied nur auf den zwei größten Positionen.
5. **a/t-Untergrenze von 404** (a / höchste faire Forderung): 1,00 / 0,55 / 0,65 / 0,78 / 0,97, eine unfaire (G2 Pos. 4, 1,40). 5 von 6 fair, aber kein systematisches „knapp unter t“. Wir: 3 von 6 unfair (G1 Pos. 1, G2 Pos. 1, G2 Pos. 4), davon die zwei größten.
6. **Reviewer-Seite: wir sind besser als 404.** Überzahlung gegenüber einem perfekten Reviewer (0,5a je abgelehnter fairer, a je angenommener unfairer Forderung), G1–3: chineseModels **408**, alpha 2.247, TRINNERS 2.371, 404 3.512, FDN 7.773. 404s b liegt, wo messbar, unter der eigenen Forderung (≈ 0,6–0,93 × a). Das kostete 404 in G2 12 Fehlablehnungen. Unser einziges Leck: G3 Pos. 6 (nicht gedeckt) drei unfaire angenommen, gleiches Fehlurteil wie 404 (Deckung zu hoch eingeschätzt), nur 116.
7. **Nicht gedeckte Positionen:** Alle Top-Teams fordern trotzdem etwas, Effekt klein (±400 je Spiel). Einzelne Teams nehmen fast alles an (FDN G1: 2.592 angenommen, alpha G3 Pos. 6). Ungetestete Vermutung: höheres a auf klar ungedeckten Positionen erntet mehr bei diesen Teams; Datenlage reicht nicht.

## Folgerungen für uns

- Mit dem jetzigen `A_MULT = 0,85` hätten wir G1 Pos. 1 mit 765 fair gefordert: geschätzt +10.700 bis +11.500 statt +2.700, also gleichauf mit 404. Der Rückstand ist im Kern schon behoben.
- `A_MULT` nicht erhöhen. Modell `a·[14·P(T ≥ a) + K_u·P(T < a)]` mit `T/m` lognormal: Optimum a/m ≈ 0,76–0,86 bei unverzerrtem m, ≈ 0,70–0,77 wenn m im Median 10 % zu hoch ist. Unsere t/m-Grenzen aus G1–3 (eigene m aus DECISIONS): [0,87; 1,0) · ≥ 0,9 · [0,63; 0,88) · ≥ 1,13 · ≥ 0,85 · < 0,9 → Median etwa 0,9. Sobald `calibrate.py` auf Philips Mac mehr Zeilen hat: bei Median t/m < 0,95 auf 0,80 senken.
- b-Regel beibehalten (beste Reviewer-Bilanz im Feld).
- Größte Position je Fall: dort wird das Spiel entschieden. Prüfen, ob die Schätzung für die teuerste Zeile mehr Sorgfalt bekommt (Selbstbehalt, Höchstgrenzen, Abschreibung). Vorschlag, nicht getestet.
- Auf Philips Mac nachsehen: unser m für G2 Pos. 1. Bei m ≤ 951 wäre 0,85 dort fair gewesen (+≈ 11.000).
