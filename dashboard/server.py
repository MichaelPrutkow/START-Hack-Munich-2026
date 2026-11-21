"""Local, read-only browser dashboard for completed Claim to Fame games."""

import argparse
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from dashboard.analysis import analyse_game, simulate_game
from dashboard.leaderboard import Leaderboard


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "dashboard" / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def read_round_records(path):
    if not path.exists():
        return {}
    records = {}
    with path.open(encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
                game = record["game"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if record.get("ok") is True and not record.get("dry"):
                records[game] = record
    return records


def local_line_details(result, record):
    if not record:
        return result
    for line in result["lines"]:
        index = str(line["index"])
        item = (record.get("items") or {}).get(index) or {}
        description = item.get("description")
        if isinstance(description, str) and description.strip():
            line["description"] = description
        tier = (record.get("line_sources") or {}).get(index) or record.get("winner")
        estimate = ((record.get("est") or {}).get(tier) or {}).get(index)
        if not estimate or not line.get("own"):
            continue
        observed = {key: estimate[key] for key in ("m", "p", "t_low", "t_high")
                    if type(estimate.get(key)) in (int, float) and math.isfinite(estimate[key])}
        if observed:
            line["own"]["estimate"] = {**observed, "tier": tier}
    return result


def suggestions(result, data, team, record):
    """Show one positive hindsight scenario per position, ranked by its lower bound."""
    if not result["complete"]:
        return result
    candidates = ({}, {}, {})
    lines = {line["index"]: line for line in result["lines"]}
    for line in result["lines"]:
        if line["inference_error"] or line["t"]["lower"] is None:
            continue
        index = line["index"]
        price = math.floor(line["t"]["lower"] * 100 + 1e-7) / 100
        candidates[0][index] = {"b": price}
        if price:
            candidates[1][index] = {"a": price}
            candidates[2][index] = {"a": price, "b": price}
    for changes in candidates:
        if not changes:
            continue
        scenario = simulate_game(data, team, changes, record=record)
        if not scenario["available"]:
            continue
        for simulated in scenario["lines"]:
            index = simulated["index"]
            if index not in changes:
                continue
            delta = simulated["delta"]
            lower = delta["lower"]
            line = lines[index]
            best = line.get("recommendation")
            if lower is not None and lower >= 0.01 and (best is None or lower > best["delta"]["lower"] + 0.005):
                line["recommendation"] = {**changes[index], "delta": delta}
    return result


class Dashboard:
    def __init__(self, source, team, *, offline=False):
        self.source = source
        self.team = team
        self.offline = offline

    def overview(self, team):
        rows, timestamp = self.source.index()
        games, teams = [], {self.team}
        for row in rows:
            data = self.source.read_game(row["id"])
            net = None
            if data:
                teams.update(data["teams"])
                if team in data["teams"]:
                    net = math.fsum((r["issuer"] == team) * r["amount"] -
                                   (r["reviewer"] == team) * r["amount"] for r in data["tx"])
            games.append({**row, "ready": data is not None, "net": net,
                          "transaction_count": len(data["tx"]) if data else None})
        return {"team": team, "teams": sorted(teams, key=str.casefold), "games": games, "updated_at": timestamp,
                "fetching": self.source.fetching, "error": self.source.error, "offline": self.offline,
                "refresh_seconds": self.source.refresh_seconds}

    def detail(self, game, team, *, changes=None, records=None, recommend=True):
        data = self.source.read_game(game)
        if data is None:
            return None
        if team not in data["teams"]:
            raise ValueError(f"{team} hat an Spiel {game} nicht teilgenommen.")
        record = None
        if team == self.team:
            if records is None:
                records = read_round_records(self.source.out / "rounds.jsonl")
            record = records.get(game)
        if changes is not None:
            return simulate_game(data, team, changes, record=record)
        result = analyse_game(data, team, record=record)
        if recommend:
            result = suggestions(result, data, team, record)
        return local_line_details(result, record)

    def report(self, after):
        games, timestamp = self.source.index()
        records = read_round_records(self.source.out / "rounds.jsonl")
        counts = defaultdict(int)
        rounds, missing, positions = [], [], []
        for game in reversed(games):
            gid = game["id"]
            if gid <= after:
                continue
            result = self.detail(gid, self.team, records=records)
            if result is None:
                missing.append(gid)
                continue
            summary = result["summary"]
            rounds.append({"game": gid, "transactions": result["transaction_count"], **summary})
            for label, count in summary["counts"].items():
                counts[label] += count
            for line in result["lines"]:
                positions.append({"game": gid, "index": line["index"], "t": line["t"], "own": line["own"],
                                  "recommendation": line.get("recommendation"), **line["summary"]})
        return {"updated_at": timestamp, "team": self.team, "after": after, "missing": missing, "rounds": rounds,
                "transactions": sum(row["transactions"] for row in rounds), "counts": dict(counts),
                "net": round(sum(row["net"] for row in rounds), 2), "positions": positions}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(content)

    def json(self, status, value):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.reply(status, body, "application/json; charset=utf-8")

    def error(self, status, error):
        self.json(status, {"error": error})

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        # Only local origins can use a service which reads team-private runner logs.
        host = self.headers.get("Host", "")
        port = self.server.server_port
        if host not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            self.error(HTTPStatus.FORBIDDEN, "Nur auf diesem Rechner verfügbar.")
            return
        if self.command == "POST" and self.headers.get("Origin") not in (None, f"http://{host}"):
            self.error(HTTPStatus.FORBIDDEN, "Diese Website darf keine Szenarien anfragen.")
            return
        url = urlsplit(self.path)
        dashboard = self.server.dashboard
        team = parse_qs(url.query).get("team", [dashboard.team])[0]
        if not team or len(team) > 128:
            self.error(HTTPStatus.BAD_REQUEST, "Ungültiger Teamname.")
            return
        if self.command == "GET" and url.path in ASSETS:
            filename, kind = ASSETS[url.path]
            try:
                self.reply(HTTPStatus.OK, (STATIC / filename).read_bytes(), kind)
            except FileNotFoundError:
                self.error(HTTPStatus.NOT_FOUND, "Datei nicht gefunden.")
            return
        try:
            if self.command == "GET" and url.path == "/api/overview":
                self.json(HTTPStatus.OK, dashboard.overview(team))
                return
            parts = url.path.split("/")
            if len(parts) in (4, 5) and parts[1:3] == ["api", "games"] and parts[3].isdigit():
                game = int(parts[3])
                if self.command == "GET" and len(parts) == 4:
                    result = dashboard.detail(game, team)
                elif self.command == "POST" and len(parts) == 5 and parts[4] == "simulate":
                    result = dashboard.detail(game, team, changes=self.scenario())
                else:
                    self.error(HTTPStatus.NOT_FOUND, "Endpunkt nicht gefunden.")
                    return
                if result is None:
                    self.error(HTTPStatus.NOT_FOUND, "Die öffentliche Abrechnung ist noch nicht vollständig verfügbar.")
                else:
                    self.json(HTTPStatus.OK, result)
                return
        except (ValueError, KeyError, TypeError) as exc:
            self.error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self.error(HTTPStatus.NOT_FOUND, "Endpunkt nicht gefunden.")

    def scenario(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("JSON erwartet.")
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 32_768:
            raise ValueError("Ungültige Anfragegröße.")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict) or not isinstance(body.get("changes"), dict):
            raise ValueError("Änderungen müssen pro Position angegeben werden.")
        return body["changes"]

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--team", default="chineseModels", help="Team des lokalen Runner-Logs und Abrufteam")
    parser.add_argument("--out", type=Path, default=ROOT / "out")
    parser.add_argument("--offline", action="store_true", help="nur vorhandene lokale Abrechnungen lesen")
    parser.add_argument("--report", action="store_true", help="Transaktionen auswerten und Bericht als JSON ausgeben")
    parser.add_argument("--after", type=int, default=0, help="mit --report nur Spiele nach dieser Nummer auswerten")
    args = parser.parse_args()
    source = Leaderboard(args.out, args.team)
    dashboard = Dashboard(source, args.team, offline=args.offline)
    if args.report:
        if not args.offline:
            source.refresh()
        print(json.dumps(dashboard.report(args.after), ensure_ascii=False, allow_nan=False, indent=2))
        if source.error:
            parser.exit(1, f"Leaderboard: {source.error}\n")
        return
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.dashboard = dashboard
        if not args.offline:
            threading.Thread(target=source.poll, daemon=True).start()
        print(f"Browser-Dashboard: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            source.stopped.set()


if __name__ == "__main__":
    main()
