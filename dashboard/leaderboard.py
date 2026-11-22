"""Read and cache only the public, settled QuantCo leaderboard."""

import json
import math
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode


API = "https://c2f.public.quantco.cloud/leaderboard/api"
TRANSACTION_FIELDS = ("issuer", "reviewer", "line_item_index", "accepted", "amount")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        pending = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)
            file.close()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def is_complete(data):
    """Verify all directed team pairs appear exactly once per position."""
    if not isinstance(data, dict):
        return False
    tx = data.get("tx") or []
    teams = set(data.get("teams") or [])
    if len(teams) < 2 or not tx:
        return False
    try:
        pairs = {(x["line_item_index"], x["issuer"], x["reviewer"]) for x in tx}
        if any(i == r or i not in teams or r not in teams for _, i, r in pairs):
            return False
        if any(type(row["line_item_index"]) is not int or row["line_item_index"] < 0
               or type(row["accepted"]) is not bool or type(row["amount"]) not in (int, float)
               or not math.isfinite(row["amount"]) or row["amount"] < 0 for row in tx):
            return False
        lines = {line for line, _, _ in pairs}
        return len(tx) == len(pairs) == len(lines) * len(teams) * (len(teams) - 1)
    except (KeyError, TypeError):
        return False


class Leaderboard:
    def __init__(self, out, seed, *, refresh_seconds=60, pause=1.0):
        self.out = Path(out)
        self.cache = self.out / "dashboard" / "public"
        self.shared_cache = self.out / "leaderboard"
        self.seed = seed
        self.refresh_seconds = max(60, refresh_seconds)
        self.pause = max(1.0, pause)
        self.fetching = False
        self.error = None
        self.stopped = threading.Event()
        self._validated = {}

    def get(self, endpoint, **params):
        if self.stopped.wait(self.pause):
            raise InterruptedError("Dashboard beendet")
        url = f"{API}/{endpoint}?{urlencode(params)}"
        result = subprocess.run(
            ["curl", "--disable", "--proto", "=https", "--silent", "--show-error", "--fail", "--max-time", "12", url],
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    def pages(self, endpoint, **params):
        page = 1
        while True:
            response = self.get(endpoint, page=page, **params)
            items = response["items"]
            if not isinstance(items, list):
                raise ValueError("Unerwartetes Antwortformat beim Leaderboard")
            yield from items
            if page >= response.get("total_pages", 1):
                break
            page += 1

    def read_game(self, game):
        name = f"game_{game:03d}.json"
        for path in (self.cache / name, self.shared_cache / name):
            try:
                stamp = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            stamp_and_data = self._validated.get(path)
            if stamp_and_data is not None and stamp_and_data[0] == stamp:
                return stamp_and_data[1]
            data = read_json(path)
            if is_complete(data) and data.get("game") == game:
                self._validated[path] = (stamp, data)
                return data
        return None

    def fetch_game(self, game):
        cached = self.read_game(game)
        if cached is not None:
            return cached
        first = list(self.pages("transactions", game_id=game, team=self.seed))
        if not first:
            return None
        teams = sorted({row[field] for row in first for field in ("issuer", "reviewer")})
        # Team views include both directions. Keep one authoritative copy per directed pair.
        transactions = {}
        for team in [self.seed] + [name for name in teams if name != self.seed]:
            rows = first if team == self.seed else self.pages("transactions", game_id=game, team=team)
            for row in rows:
                key = tuple(row[field] for field in ("line_item_index", "issuer", "reviewer"))
                clean = {field: row[field] for field in TRANSACTION_FIELDS}
                if key in transactions and transactions[key] != clean:
                    raise ValueError(f"Widersprüchliche Abrechnung in Spiel {game}")
                transactions[key] = clean
        data = {"game": game, "teams": teams, "tx": list(transactions.values())}
        if not is_complete(data):
            raise ValueError(f"Unvollständige Abrechnung in Spiel {game}")
        atomic_json(self.cache / f"game_{game:03d}.json", data)
        return data

    def index(self):
        remote = read_json(self.cache / "index.json") or {}
        games = {row["id"]: row for row in remote.get("games", [])}
        for directory in (self.shared_cache, self.cache):
            for path in directory.glob("game_*.json"):
                suffix = path.stem.removeprefix("game_")
                if suffix.isdecimal() and int(suffix) > 0:
                    game = int(suffix)
                    if game not in games and self.read_game(game) is not None:
                        games[game] = {"id": game, "start_time": None, "status": "completed"}
        return sorted(games.values(), key=lambda row: row["id"], reverse=True), remote.get("updated_at")

    def refresh(self, backfill_seconds=None):
        self.fetching = True
        start = time.monotonic()
        try:
            games = [
                {"id": game["id"], "start_time": game.get("start_time"), "status": "completed"}
                for game in self.pages("games", completed_only="true", page_size=1000)
                if game["id"] > 0 and game.get("status", "completed") == "completed"
            ]
            atomic_json(self.cache / "index.json", {"games": games, "updated_at": datetime.now(timezone.utc).isoformat()})
            errors = []
            for game in sorted(games, key=lambda row: row["id"], reverse=True):
                if self.stopped.is_set() or (backfill_seconds is not None and time.monotonic() - start >= backfill_seconds):
                    break
                try:
                    if self.fetch_game(game["id"]) is None:
                        errors.append(f"Spiel {game['id']} wartet auf Transaktionen")
                except (subprocess.SubprocessError, ValueError, KeyError, OSError) as exc:
                    if self.stopped.is_set():
                        break
                    errors.append(f"Spiel {game['id']}: {self.describe_error(exc)}")
                    if isinstance(exc, subprocess.SubprocessError):
                        break
            self.error = errors[0] if errors else None
        except (subprocess.SubprocessError, ValueError, KeyError, OSError) as exc:
            self.error = self.describe_error(exc)
        finally:
            self.fetching = False

    @staticmethod
    def describe_error(exc):
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            return exc.stderr.decode(errors="replace").strip()
        return str(exc)

    def poll(self):
        while not self.stopped.is_set():
            start = time.monotonic()
            self.refresh(backfill_seconds=40)
            self.stopped.wait(max(1, self.refresh_seconds - (time.monotonic() - start)))
