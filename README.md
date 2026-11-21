# Claim to Fame runner

Setup (einmal):
1. `cp .env.example .env`, dann in `.env` eintragen: `TEAM_API_KEY=...`, `OPENAI_KEY_1=...` (weitere Keys als `_2`, `_3`). Keine Anführungszeichen nötig. `.env` ist gitignored.
2. Alle Fall-Zips aus dem Drive-Ordner nach `cases/` kopieren (Name egal, Hauptsache die Spielnummer steht im Dateinamen, z. B. `case_07.zip`).
3. `uv run runner.py --check` zeigt: Keys gesetzt, Modell erreichbar, nächstes Spiel, fehlende Zips.

Test: `uv run runner.py --game 0` (Spiel 0 speichert nichts). `--no-llm` testet den Notbetrieb, `--dry` sendet nichts.

Spielen: `caffeinate -i uv run runner.py` auf GENAU EINEM Rechner (Lockfile schützt nur diesen Rechner). Logs: `out/runner.log`, Abgaben `out/submissions.jsonl`, Runden inkl. Token `out/rounds.jsonl`.

Ablauf pro Runde: Key ab T+0 → entschlüsseln (7z, sonst pyzipper) → PDF parsen → vorläufige Abgabe aus eigenen groben Sätzen je Einheit (a = b) → ein OpenAI-Call mit n=3 Stimmen (Structured Outputs) → a = Median (bei Uneinigkeit geom. Mittel der zwei höchsten), auf Cent abgerundet; b = a bei Deckung > 2/3, sonst 0 → finale Abgabe → Echo-Check. Key-Cascade: 401/insufficient_quota sperrt den Key fürs Turnier, 429 Rate-Limit für die Runde. Ohne Key oder LLM bleibt die Regel-Abgabe stehen, nie 0/0.
