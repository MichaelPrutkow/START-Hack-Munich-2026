# Briefing-Abgleich · 21.11.2026

Geprüft wurden `Claim_to_Fame_Team_Briefing.pdf` (internes Vorbereitungspapier), `GAME_DESCRIPTION.pdf`, `EVALUATION_CRITERIA.pdf` und `Claim to Fame – Challenge Deep Dive (10 min).pdf`. Das interne Papier erklärt auf Seite 1 seinen August-Bezug; es ist keine zusätzliche offizielle November-Spezifikation.

## Umgesetzte Lücken

| Thema | Vorher | Jetzt |
| --- | --- | --- |
| Parameterlernen (Team-Briefing S.14, 23) | Bias/σ vorhanden, A_MULT fest | Bias/σ aus zensierten Trainingsintervallen; A_MULT-Auswahl nach konservativem Trainings-Netto; unveränderte Mindestmengen und getrennte spätere Prüfung. |
| Automatische Rückkopplung (S.14, 28–30) | Separater Watcher musste mit `--learn` laufen | Ein Hintergrund-Lerner im geplanten Runner, Zeitabstand zum Spielfenster, wiederaufnehmbare öffentliche Downloads und atomare private Artefakte. |
| Abrechnungsprüfung (S.14, 28, 30) | Vollständigkeits-/Preisprüfung ohne unabhängigen Nettoabgleich | Transaktionen zusätzlich gegen öffentliche Spiel-/Team-Matrixzellen geprüft, Toleranz 0,02 €. Fehlende Zellen werden als nicht verfügbar markiert, Widersprüche ausgeschlossen. |
| Fortlaufende Datennutzung | Änderungen am ganzen Runner trennten Kohorten; kalibrierte Zeilen wurden verworfen | Identität je Schätzer/Stufe; rohe Schätzung und tatsächlich angewandte Parameter erlauben erneutes Lernen ohne Bias-Vervielfachung. |
| Alte Logs | Ausschluss ohne `final_ab` | Nur vollständige explizite Gewinnerstufe erfolgreicher alter Abgaben rekonstruieren und gegen Abrechnung prüfen; keine erfundenen Modell-/Prompt-Identitäten. |
| Nachvollziehbarkeit | Verlustbericht, wenig Erklärung bei inaktiver Kalibrierung | Gründe, Mengen, vorgeschlagene/aktive Parameter, gültige Vorgänger und Feldraten für bekannte faire/unfaire Forderungen. |

Neue Parameter brauchen mindestens sechs passende Spiele, 16 bestätigt gedeckte Trainingszeilen und acht spätere Prüfzeilen. Die spätere Prüfung enthält auch falsche Deckungsprognosen innerhalb des Anwendungsbereichs. Die konservative Gewinnuntergrenze muss den tatsächlichen Vergleichswert um mindestens 5 % übertreffen. Ein Ersatz muss zusätzlich den bisher aktiven Parametersatz auf denselben Prüfspielen schlagen. Unveränderte Daten verlängern keine Validierung. Diese Implementierung belegt noch keinen Mehrgewinn im Turnier.

## Noch offene Experimente

- **Kalibrierte Unsicherheit und Deckungswahrscheinlichkeit:** Stimmenstreuung ist keine gemessene Treffergenauigkeit. Validierte Vorhersageintervalle und Zuverlässigkeitskurven fehlen (Team-Briefing S.9, 17, 24).
- **Recall ohne gegenseitige Bestätigung:** Eine blinde Kontrollstimme und ein Vergleich mit/ohne historischen Kontext fehlen. Die Warnung auf S.16 spricht gegen die Behauptung, Konsens allein bedeute Genauigkeit.
- **Mehr Betriebsnachweise:** Tokenverbrauch wird protokolliert; gemessene Eurokosten, Latenzvergleich, Top-Verlust-Dashboard und manueller Eingriff sind noch keine vollständige Auswertung (S.11, 18, 25, 30–31).
- **Ökonomische Sensitivität:** Eigene historische Forderungen verengen teilweise die bekannten Preisgrenzen. Ein zusätzlicher Vergleich ohne diese eigenen Signale kann die Belastbarkeit zeigen. Die aktuelle Auswertung bewahrt Grenzen, statt den geheimen fairen Betrag zu schätzen.

Das sind überwiegend Empfehlungen des internen Briefings. Offiziell zählen Rang/Netto (70 %) und Methode, Belege, Ressourceneinsatz, Zuverlässigkeit und Ehrlichkeit (30 %; Evaluation Criteria S.1).

## Veraltete Annahmen nicht übernehmen

- Abgelehnte **faire** Forderungen zahlen im November **1,5 × a**; daher `a = amount / 1,5` bei positiver Ablehnungszahlung. Die August-Aussagen auf S.6/13 des Team-Briefings passen nicht zu Game Description S.2 und Deep Dive S.4.
- Einseitige Preisgrenzen bleiben zensiert. Keine Ersetzung durch `1,35–1,6 × Untergrenze` aus dem internen Papier.
- Keine August-Preisanker, keine unbelegte Dreifachgewichtung später Runden, keine Übernahme historischer Gewinnbehauptungen.
- Das offizielle Write-up hat **höchstens eine Seite**, nicht ein bis zwei (Game Description S.4; Evaluation Criteria S.1).

Die Änderungen werden ausschließlich offline mit synthetischen Abgaben, gefälschten HTTP-Antworten und lokalen Dateien geprüft. Der Live-Runner und die aktuellen Rundenlogs liegen weiterhin bei Philip; nur dort lässt sich prüfen, welche Parameter nach genügend passenden November-Runden tatsächlich aktiv werden.
