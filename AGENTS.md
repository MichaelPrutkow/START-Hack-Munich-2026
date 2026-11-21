# START Hack Munich 2026 – gemeinsame Übergabe für Codex und Claude

## Zusammenarbeit im selben Team

- Lies diese `AGENTS.md` bei jeder neuen Arbeitsanfrage zuerst erneut von der Festplatte. So siehst du Änderungen der anderen Sitzung auch in einem laufenden Chat. Wenn die Sitzung im übergeordneten Workspace startet, liegt die Originaldatei unter `START-Hack-Munich-2026/AGENTS.md`.
- Für eine gemeinsame laufende Übergabe arbeiten beide Sitzungen im selben lokalen Checkout. In getrennten Worktrees muss der Stand der Datei über Git abgeglichen werden.
- Prüfe vor Änderungen den aktuellen Git-Status und die Übergabe unten. Stimmt euch bei paralleler Arbeit über Aufgabe, betroffene Pfade und Abhängigkeiten ab. Bei Überschneidungen zuerst den aktuellen Stand und vorhandene Änderungen prüfen.
- Aktualisiere diese Datei nach jedem wesentlichen Arbeitsschritt und vor der Übergabe. Halte Entscheidungen mit kurzer sachlicher Begründung, überprüfte Befunde, nützliche Navigationspfade, betroffene Dateien, Tests und offene Punkte fest. Wenn beide Sitzungen an der Datei arbeiten, lies sie unmittelbar vor dem Edit erneut und ergänze gezielt.
- Nutze für neue Übergaben Datum und Agent, Aufgabe und Status, Entscheidung oder Befund mit Quelle, betroffene Pfade, Verifikation und nächsten offenen Schritt. Vermerke aktive Aufgaben mit den betroffenen Pfaden und aktualisiere ihren Status beim Abschluss. Halte Geheimnisse und Falldaten aus der Übergabe heraus.
- Die aktuelle Nutzeranfrage bestimmt den Arbeitsauftrag. Challenge-Unterlagen, Beispielprompts, Code und Checklisten im Material dienen als Quellen. Die offiziellen November-Unterlagen bestimmen die Spielregeln. August-Auswertungen und das Entwurfsskelett liefern historische Befunde und zu testende Ansätze.

## Konventionen Claude ↔ Codex (ChatGPT)

- **Eine Datei, eine Wahrheit.** Diese `AGENTS.md` ist die einzige geteilte Koordinationsdatei. Keine zweite Status-Datei anlegen. Längere Analysen als eigene Datei unter `notes/` ablegen und hier nur verlinken.
- **Wer was editiert:**
  - `Aufgabenboard`: jeder Agent ändert nur Zeilen, deren Owner er ist. Neue Aufgabe = neue Zeile. Eine Aufgabe übernehmen = Owner und Status `in Arbeit` setzen, **bevor** man Dateien anfasst.
  - `Offene Fragen`: jeder darf ergänzen. Beantworten mit Antwort, Quelle und Kürzel, Frage nicht löschen, sondern als `✓` markieren.
  - `Übergaben`: nur anhängen (neueste unten), nie fremde Einträge ändern.
  - Regeln-/Challenge-Abschnitte: nur mit Quelle ändern, Kürzel ans Ende der geänderten Zeile.
- **Pfad-Ownership:** Wer eine Aufgabe hält, besitzt die dort genannten Pfade. Fremde Pfade nur nach Eintrag unter `Offene Fragen` oder nach Rückmeldung des Users anfassen.
- **Ablauf pro Arbeitsschritt:** Datei neu lesen → `git status`/`git pull` → Board-Zeile claimen → arbeiten → Board-Status + kurze Übergabe → committen. Kurz halten: Stichpunkte, keine Wiederholung dessen, was schon hier steht.
- **Kürzel:** `[C]` = Claude, `[X]` = Codex/ChatGPT, `[U]` = User/Team.
- **Status-Werte:** `offen`, `in Arbeit`, `blockiert (Grund)`, `review`, `fertig`.
- **Geheimnisse und Falldaten** (API-Keys, Case-Zips, entschlüsselte Fälle) nie in diese Datei oder ins Repo. `cases/`, `out/`, `.env` gehören in `.gitignore`.

## Zugeteilte Challenge: QuantCo „Claim to Fame“

Zuteilung 21.11. 13:39: **QuantCo**. revel8, Lio und die übrigen Partner sind damit irrelevant. [U/C]
Erste Runde Sa 14:00, dann alle 13 min bis So 12:00 (≈ 101 Runden). Write-up/Abgabeformular So 12:00–12:45, Demos 13:15–14:45 (3 min Pitch + 2 min Q&A). Preis laut QuantCo-Folie: AirPods und Merch. [C]

### Stand Repo und Material (21.11. 13:45)

- Offizielles Material liegt jetzt im Workspace-Ordner `start_hack_munich/` (eine Ebene über dem Repo): `API_HANDBOOK.md`, `starter_script.py`, `pixi.toml`, `GAME_DESCRIPTION.pdf`, `EVALUATION_CRITERIA.pdf`, Deep-Dive-PDF, `Hackathon presentation 21.11.26.pdf`. Inhalt deckt sich mit den Markdown-Abschriften in `~/Downloads/starthack 2/offiziell/`. [C]
- API-Handbuch, neu gegenüber den Folien: ausgelassene Positionen nehmen trotzdem teil (0/0); 422 bei negativen oder nicht endlichen Werten; 403 auch „Game has already ended“; Team, das nach Spielstart angelegt wurde, spielt erst ab dem nächsten Spiel. Basis-URL `https://c2f.public.quantco.cloud`. [C]
- Fall-Zips: verschlüsselt im Drive, Passwort (= `decryption_key`) kommt pro Spiel über `GET /api/games/{id}/key` ab `start_time`. Zips müssen **vorher** lokal nach `cases/` (Spielnummer im Dateinamen). Alle 101 Zips (0–100) liegen im Drive, AES-verschlüsselt, pyzipper öffnet sie ohne 7z (geprüft 21.11. 13:46). [C]
- `origin/main` enthält bereits `runner.py` (fertiger Rundenlauf: Key → 7z/pyzipper → PDF parsen → vorläufige Regel-Abgabe → LLM-Votes → finale Abgabe → Echo-Check), `pyproject.toml` (uv), `.env.example`, `.gitignore` (`.env`, `cases/`, `out/`, `*.zip`). Zusätzlich Remote-Branch `origin/runner`. [C]

## Live-Betrieb (Stand 21.11. 14:32) [C]

- **Team:** `chineseModels`. **Runner:** nur Philips Mac. Setup und Startbefehl stehen in `README.md` (`brew install uv sevenzip`, `uv sync`, dann `source .venv/bin/activate && python3 runner.py --check && caffeinate -i python3 runner.py`). Spiele alle 800 s ab 14:00:00 (14:13:20, 14:26:40, 14:40:00 …), Fenster 60 s. Neustart nur in den Minuten dazwischen.
- **Preisregel** (`runner.py: price`): `a = A_MULT × m`, `b` so dass `p · P(T ≥ b) = 2/3` mit `T ~ m·exp(SIGMA·Z)`, `b = 0` bei `p ≤ 2/3`. Einstellbar per `.env` (nur lokal, nicht in Git): `A_MULT` (Default jetzt 0,85, seit Spiel 3 aktiv), `SIGMA` (0,35). Spiel 3 mit 0,85: +4.528, bestes Team der Runde.
- **Befund Spiel 1–2:** Unser b war fast perfekt. Unser a lag in 3 von 4 gedeckten Positionen knapp **über** t (LLM überschätzt t um ~10–15 %). Vier Teams geben gar nicht ab und lehnen alles ab → jede faire Forderung bringt von ihnen 1,5a, jede überhöhte nichts.
- **Kalibrierung:** `uv run calibrate.py --team chineseModels` (liest nur, cached in `out/leaderboard/`, 1 Anfrage/s). Für t/m und Backtest auf Philips Rechner laufen lassen, dort liegt `out/rounds.jsonl` (ab dem Update mit `est` je Position).
- **Tests:** `uv run python test_runner.py` (offline, Parser gegen Case-0-PDF und Preisregel).
- **Entscheidungen mit Messwerten:** `notes/DECISIONS.md`. Jede Parameteränderung dort mit Beleg und Ergebnis eintragen (Basis fürs Write-up).

## Aufgabenboard

| # | Aufgabe | Owner | Status | Pfade |
|---|---|---|---|---|
| 1 | Projektkontext und Quellen auswerten | [X] | fertig | `AGENTS.md` |
| 2 | Board, Konventionen, Challenge-Stand | [C] | fertig | `AGENTS.md` |
| 3 | `AGENTS.md` committen und pushen | [U] | fertig | `AGENTS.md` |
| 4 | `.env` füllen, alle Fall-Zips aus dem Drive nach `cases/` | [U] | fertig | `.env`, `cases/` |
| 5 | `uv run runner.py --check`, dann `--game 0` (Formattest) **vor 14:00** | [C] | fertig (13:46, Echo ok) | `runner.py` |
| 6 | Genau einen Runner-Rechner bestimmen, `caffeinate -i uv run runner.py` starten | [U] | fertig: **nur Philips Mac** | `out/` |
| 7 | Review `runner.py`: Parser gegen Case-0-PDF, Brutto/MwSt., Strategie a/b | [C] | fertig | `runner.py`, `test_runner.py` |
| 8 | Auswertung der ersten Runden (Leaderboard), Kalibrierung von a/b | [C] | in Arbeit | `calibrate.py` |
| 9 | Write-up (1 Seite) und Pitch | – | offen | `writeup/` |
| 10 | Runner modularisieren, Einstiegspunkte erhalten, offline verifizieren | [X] | fertig | `runner.py`, `claim_to_fame/`, `eval_cases.py`, `test_runner.py`, `tests/`, `README.md`, `.env.example` |
| 11 | Michaels neue Commits mit lokalem Refactor zusammenführen, offline prüfen, nicht pushen | [X] | fertig | `AGENTS.md`, `README.md`, Merge von `origin/main` |

## Offene Fragen

- [x] Welche Challenge? → QuantCo. [U]
- [ ] Zählen späte Runden mehrfach (August: letzte 20 dreifach)? → Orga/Discord fragen. [X]
- [ ] Höhe der absoluten Untergrenze von `c`. [X]
- [ ] Dürfen August-Preisanker und automatisiert gelesene Leaderboard-Transaktionen genutzt werden? → Orga fragen. [X]
  - ✓ Leaderboard: Scrapen erlaubt, nur nicht im Millisekundentakt (Orga, mündlich an Michael, 21.11. 14:11). August-Daten: weiter nicht nutzen (Michaels Regel). [U/C]
- [ ] Format und genaue Frist des Write-ups (max. 1 Seite). [X]
- [x] Wer ist der eine Runner? → Philips Mac. Auf keinem anderen Rechner `runner.py` starten (auch nicht `--game`), sonst zwei Abgaben. [U]
- [ ] Ist `MichaelPrutkow/START-Hack-Munich-2026` privat? Falldaten dürfen nicht öffentlich werden. [C]
- [ ] Liegen alle Fall-Zips (bis So 12:00) schon im Drive oder kommen sie nach und nach? [C]

## Material finden

Die beigefügten Quellen liegen auf diesem Mac unter `~/Downloads/starthack 2/`. Die Pfade in der Tabelle sind relativ zu diesem Ordner. Der gesichtete Bestand dokumentiert QuantCos **Claim to Fame**. Weitere Challenges sind im Bestand nicht belegt.

| Thema | Quelle |
| --- | --- |
| Offizielle Regeln, Ablauf und Bewertung | `offiziell/Game_Description.md`, `offiziell/Deep_Dive_Slides.md`, `offiziell/Evaluation_Criteria.md`. Daneben liegen die drei Original-PDFs. |
| Unterschiede August/November | `10_Aenderungen_November_vs_August.md` |
| Quellenverzeichnis und historische Repos | `00_Quellen_Repos.md` |
| August-Spielregeln, API-Fallen und Mathematik | `01_Regeln_Auszahlung_Ablauf.md`, `02_API_Datenformat_Fallen.md`, `03_Mathematik_Strategie.md` |
| Abrechnung, Kalibrierung und Recall | `04_Leaderboard_Klammern_Recall.md`. Verfügbarkeit und Spielregeln für die Nutzung der betreffenden Daten im November prüfen. |
| Historische Teamresultate, Fehler und Preisanker | `05_Ergebnisse_Teams_August.md`, `06_Learnings_Fehlerkatalog.md`, `07_Fachwissen_Preisanker.md`, `preisanker_august.csv` |
| Pipeline, Prompt und ungetestetes Python-Skelett | `08_Architektur_Prompt_Code.md` |
| Pitch und Write-up | `09_Bewertung_Pitch_Writeup.md`, für die aktuellen Vorgaben zusätzlich `offiziell/Evaluation_Criteria.md` |

## QuantCo – bestätigte November-Regeln

- Pro Rechnungsposition schickt das Team Forderung `a` und Annahmelimit `b`, jeweils als **Brutto-Gesamtbetrag** inklusive MwSt. Die Annahme erfolgt bei `a ≤ b`. `t` ist der geheime faire Höchstbetrag und beträgt bei fehlender Deckung null. Zahlung vom Reviewer an den Issuer: bei Annahme `min(a, c)`, bei Ablehnung einer fairen Forderung (`a ≤ t`) `1,5 × a`, sonst `0`. Das Ergebnis ist nullsummig. Das geheime Zahlungslimit `c` erfüllt `c ≥ 4t` und hat eine zusätzliche absolute Untergrenze unbekannter Höhe.
- Die Folien nennen Samstag, 21.11., 14:00 Uhr bis Sonntag, 22.11., 12:00 Uhr (München), im 13-Minuten-Takt. Jede Runde erlaubt 60 Sekunden. Die letzte rechtzeitige Abgabe zählt. Fehlende Positionen erhalten `a = b = 0`. Die offizielle Rundenliste liefert die Startzeiten.
- Team-API laut Folien: `GET /api/games/list`, `GET /api/games/{id}/key` ab Rundenstart und `PUT /api/games/{id}/submissions` mit `X-API-Key` und einer Liste von Objekten mit `index`, `charge_price`, `acceptance_limit`. Spiel 0 validiert das Abgabeformat und speichert nichts. Case-Zips aus dem geteilten Drive enthalten Police, Schadenbeschreibung, Rechnung und ggf. Bilder. Die Game Description nennt `starter_script.py`, `pixi.toml` und `API_HANDBOOK.md`. Diese Dateien und die Case-Zips waren im übergebenen Ordner nicht vorhanden.
- 70 Punkte werden linear nach Rang im kumulierten Nettoergebnis vergeben, vom ersten Platz mit 70 bis zum letzten mit 0. Die Jury vergibt bis zu 30 Punkte für Methode und Unsicherheit, Belege für Entscheidungen, sparsamen Ressourceneinsatz, zuverlässige Automatisierung sowie Klarheit und Ehrlichkeit. Write-up höchstens eine Seite, fällig kurz vor Turnierende, ergänzt durch einen Pitch.
- Laut Fair Play bleiben Strategie, Fallanalyse und Schlüssel im Team. Nutzt die vorgesehenen API-Endpunkte und Freigabezeiten. Klärt mit der Orga die Nutzung historischer Preisanker und automatisierter Transaktionsdaten. Gewichtung später Runden sowie genaue Write-up-Frist und Abgabeformat sind offen.

## Technische Schlüsse für spätere Aufgaben

- Vollständige und rechtzeitige Abgabe zuerst testen. Ein Runner, alle Rechnungsseiten und echten Positionsnummern inklusive Lücken, eine vollständige vorläufige Abgabe und ein finaler Lauf mit Zeitreserve sind sinnvolle Startpunkte aus den August-Postmortems. Spiel 0 eignet sich zur Formatprüfung. Die Prüfung gespeicherter Werte braucht separate Verifikation.
- Deckung und Schadenbezug anhand der vollständigen Police beurteilen. Marktwert bei Deckung separat schätzen. Klausel, bekannte Betragsgrenzen und Unsicherheit strukturiert ausgeben lassen. `a` und `b` deterministisch berechnen und bei Bedarf gegen Beispiele und Messungen kalibrieren. Die August-Experimente legen schnelle Modellaufrufe und einen regelbasierten Fallback nahe. Ihr Nutzen für November ist offen.
- Für eine konkrete Forderung `a > 0`, bei der die Zahlung bei Annahme ungekürzt wäre, liegt die Annahmeschwelle für `P(t ≥ a)` bei `2/3` (Gleichstand bei `2/3`). Das ist eine Aussage über Fairness bei diesem Betrag. Die reine Deckungswahrscheinlichkeit liefert diese Größe allein nicht.
- Falls freigegebene November-Abrechnungsdaten das August-Schema verwenden und `amount` die Issuer-Auszahlung ist, gilt für abgelehnte faire Zeilen mit positivem `amount`: `a = amount / 1,5`. Bei Annahme kann `amount` durch `c` gekappt sein. Abrechnung gegen offizielle Ergebnisse prüfen und die Nullsumme erst über alle Teams prüfen. Historisches Recall nur nach Freigabe, für Preise und mit passender Police, Position und Einheit testen.

## Übergaben

### 21.11.2026 · Codex · Projektkontext eingerichtet

- Die Übergabe liegt im Git-Repo. `CLAUDE.md` importiert sie für Claude Code. Der übergeordnete lokale Workspace hat Einstiegspunkte für beide Programme. Aktualisierungen gehören in diese `AGENTS.md`.
- Quelleninventar und offizielle Markdown-Abschriften ausgewertet. Der Repo-Ausgangsstand enthält `README.md` und `.gitignore`. Eine Implementierung oder ein Live-Test der Challenge war nicht Teil des Auftrags.
- Codex-Diagnose hat die Datei im Prompt aus beiden Ordnern bestätigt. Beide Claude-Importpfade und die Quellenpfade sind geprüft. In einer laufenden Claude-Code-Sitzung kann der Import unter `/memory` geprüft werden.

### 21.11.2026 · Claude · Challenges-Überblick und Koordinationsregeln

- `AGENTS.md` bleibt die einzige geteilte Datei (statt einer zusätzlichen `SHARED_CONTEXT.md`). Ergänzt: Konventionen, Challenges-Überblick, Aufgabenboard, Offene Fragen.
- revel8 und Lio haben keine öffentlichen Aufgaben. Gesucht: Event-Seite app.hackleague.org/events/start, hackathonradar, Feyer-Repo. Nur Firmenprofile eingetragen.
- Nächster Schritt: Zuteilung abwarten (12:55), dann Board-Zeilen 4–7 verteilen.

### 21.11.2026 · Claude · Zuteilung QuantCo, Repo-Stand, AGENTS.md wiederhergestellt

- Challenge QuantCo eingetragen, revel8/Lio entfernt. Board auf Turnierbetrieb umgestellt (Zeilen 3–9).
- Offizielles Material im Workspace und `runner.py` auf `origin/main` gesichtet, Befunde oben unter „Stand Repo und Material“.
- `AGENTS.md` war durch `git reset` auf `origin/main` gelöscht und wurde aus Claudes letzter Version wiederhergestellt (inkl. Codex-Ergänzungen bis 13:38).
- Nächster Schritt: Zeilen 3–6 vor 14:00 durch das Team, danach Review `runner.py` (Zeile 7).

### 21.11.2026 14:27 · Claude · Runner verifiziert, Preisregel, Kalibrierung

- Runner gegen echtes `API_HANDBOOK.md` geprüft (Header, Endpunkte, PUT-Format, Echo über `line_item_index`): passt. Spiel 0 komplett durchgespielt, 4 Positionen, Echo ok.
- Parser hängt umbrochene Beschreibungszeilen an und nimmt die letzte Zahl+Einheit der Zeile. Preisregel a/b neu (siehe Live-Betrieb), Begründung per Auszahlungssimulation. `calibrate.py` neu, Runner loggt `est` (p, m) je Position.
- Nächster Schritt: nach 5–10 Spielen `calibrate.py` auf Philips Rechner laufen lassen und `A_MULT`/`SIGMA` aus echten Daten setzen.

### 21.11.2026 14:36 · Claude · Analyse Team 404 (Spiele 1–3)

- Befunde mit Zahlen in `notes/TEAM_404.md` (nur gecachte Leaderboard-Daten, keine neue Anfrage, kein Runner-Code geändert).
- Kern: 96 % unseres Rückstands auf 404 stammen aus G1 Pos. 1 (unser a = 900 knapp über t, 404 = 780,69 knapp darunter). Mit `A_MULT = 0,85` wäre die Zeile fair gewesen. Unsere Reviewer-Bilanz ist die beste im Feld.
- Nächster Schritt: auf Philips Mac m für G2 Pos. 1 prüfen; `A_MULT` 0,85 halten, bei Median t/m < 0,95 auf 0,80.

### 21.11.2026 14:58 · Claude · Audit des Algorithmus (nur gelesen)

- Bericht und Patch liegen gitignored unter `out/audit/` (enthalten Falldetails): `AUDIT_2026-11-21.md`, `runner_audit.patch` (gegen `298e11f`, nicht angewendet, `git apply --check` ok, Tests ok in Kopie), `censored_fit.py`.
- Kern: Rechnungen haben keine Preise, m kommt immer vom LLM. Verluste stammen aus m auf der größten Position, nicht aus p. Gemeinsame (anteilige) Höchstgrenzen über mehrere Positionen kennt das Schema nicht. `calibrate.py` schätzt t/m nur aus beidseitig begrenzten Zeilen (Zensierung); zensiert gerechnet Median t/m ≈ 1,0, log-sd ≈ 0,5. A_MULT 0,85 und SIGMA 0,35 lassen.
- Nächster Schritt: Patch offline gegen Fälle 1–4 prüfen (eigenes Skript, nicht `runner.py --game`), dann in einer Spielpause auf Philips Mac einspielen.
- Nachtrag 15:00 [C]: Audit mit August-Notizen (`alte_daten` 03/04/06/07/08/10) abgeglichen, Abschnitt 6 im Bericht. Lean-high-Abschaltung zurückgezogen (August +81 k), Fotos gestrichen (+22 % ohne). Neu im Patch: Fachregeln aus 08 §4 (neu für alt, Betterment positiv, Vorsorge), `calibrate.py --tier fast|deep`. Nächste Messung ohne Token: fast gegen deep auf Philips `rounds.jsonl`.

### 21.11.2026 15:12 · Claude · Prompt v2, Sammelgrenzen, deep 3 Stimmen umgesetzt

- `runner.py`: Klausel vor den Zahlen, Fachregeln im Prompt, Policen-Beträge per Regex am Prompt-Ende, `shared_cap` + `apply_shared_caps` (anteilige Kürzung), `DEEP_VOTES` Default 3, `DISAGREE` per `.env`. `calibrate.py`: zensierte t/m-Schätzung, `--tier`. Neu `eval_cases.py` (Offline-Replay, gibt nichts ab, wartet außerhalb der Spielfenster, kostet Token).
- Beleg und verworfene Regel: `notes/DECISIONS.md` Zeile 15:10. Tests: `test_runner.py` 6/6, Offline-Smoke-Test von `play_game` (dry, Fake-LLM).
- Nächster Schritt: Philip `git pull`, in `.env` `DEEP_VOTES=3` setzen (oder Zeile löschen), Neustart zwischen zwei Spielfenstern, danach `calibrate.py --tier deep` beobachten.

### 21.11.2026 15:21 · Codex · Runner modularisiert und offline verifiziert

- Auftrag [U]: neuesten Push prüfen, Struktur verbessern, Änderungen auflisten; ausdrücklich **nicht pushen**, zusätzlich pullen. `git fetch origin` und `git pull --ff-only` erfolgreich: `6cd9340`, beim Pull bereits aktuell.
- `runner.py` von 755 auf 531 Zeilen reduziert: Parser/Fallback nach `claim_to_fame/invoices.py`, Prompt/Schema nach `claim_to_fame/prompts.py`, Aggregation/Limits/Preisregel nach `claim_to_fame/pricing.py`. Bisherige CLI und `runner`-Hilfsfunktionen bleiben verfügbar; Laufzeiteinstellungen werden explizit an die Preislogik weitergereicht.
- `eval_cases.py` lädt historische Module zusammen mit dem gewählten Git-Commit und stellt danach die aktuellen Imports wieder her. So mischt der Offline-Vergleich keine alten und neuen Implementierungen. README enthält Modulübersicht und Ablauf für spätere Team-Pushes; `.env.example` nur mit aktualisiertem Pfadhinweis.
- `test_runner.py` startet die Suite unter `tests/`: 13 Tests, davon 12 bestanden und 1 mangels entschlüsselter Case-0-PDF übersprungen. Neue Prüfungen: Laufzeiteinstellungen, unvollständige/fehlgeschlagene LLM-Antworten, Tier-Reihenfolge und getrennte historische Imports (inkl. Fehlerfall).
- Verifikation: `.venv/bin/python -B test_runner.py`, `git diff --check`; zusätzlich 2.000 Preis-, 500 Aggregations- und 500 Shared-Cap-Vergleiche identisch zu `6cd9340`, Prompt/Schema identisch und verbliebene Laufzeitfunktionen strukturell unverändert. Kein Live-Runner gestartet, keine API-/LLM-Aufrufe.
- Änderungen werden nur lokal committet. Nächster Schritt: Michaels nächsten Push abrufen, Änderungen an verschobenen Blöcken in die jeweiligen Module integrieren, Offline-Suite erneut ausführen und dem User vor dessen Push zeigen. `calibrate.py` bleibt unverändert bei [C].

### 21.11.2026 15:25 · Claude · 404-Analyse bis Spiel 6

- `notes/TEAM_404.md` um Spiele 4–6 ergänzt (Cache, keine neue Anfrage, kein Runner-Code). Rückstand 17.163: 98 % aus G1 Pos. 1 und G6 Pos. 1. In G6 forderte 404 743,99 (1,63 × Median, fair), wir 467,50 (m = 550, t ≥ 744).
- Muster: 404 ist auf großen Zeilen öfter die höchste noch faire Forderung; unser m streut stark (t/m ≥ 1,35 bis < 0,85), Verzerrung ist nicht das Problem. A_MULT 0,85 lassen, neue Version ab Spiel 7/8 an der größten Zeile messen.
- Nachtrag 15:30 [C]: `watch.py` (nur Leaderboard, kein LLM) läuft auf Michaels Mac im Terminal-Tab „Wächter“: Bericht je Spiel, macOS-Alarm bei fehlender Abgabe, Log `out/watch.log`. Eval-Summe korrigiert auf Zeilen mit Klammer (DECISIONS 15:10).

### 21.11.2026 15:28 · Codex · Divergierte Branches lokal zusammengeführt

- Nach dem gemeldeten Pull-Fehler `origin/main` erneut abgerufen: `9fbd55e`, `f0ddea9` und der inzwischen eingetroffene `7221b47`. Merge erhält beide Historien und den modularen Refactor `94a449c`; kein Rebase und kein Push.
- Nur Dokumentationskonflikte in `README.md` und `AGENTS.md`: Watcher-Erklärung, Modulübersicht und beide Übergaben erhalten. `watch.py` und `notes/DECISIONS.md` unverändert vom Remote übernommen. README ergänzt den expliziten Pull-Befehl `git pull --no-rebase`; keine globale Git-Konfiguration geändert.
- Verifikation: `.venv/bin/python -B test_runner.py` — 12 bestanden, 1 mangels Case-0-PDF übersprungen; zusätzlicher Offline-Smoke-Test von `watch.report` mit synthetischen Transaktionen bestanden. Keine API-/LLM-Aufrufe, kein Runner oder Watcher gestartet.
- Status: lokal integriert und getestet. Nächster Schritt: User prüft den Merge; Push bleibt ausdrücklich ausgesetzt.
