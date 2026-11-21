"""Bounded between-round research on recorded conditional-price estimates.

No new inference calls, submissions or automatic deployment. A candidate changes m
only; p, pricing parameters and the original recall remain frozen. Chronological
selection uses earlier settlements only. No interval midpoint is treated as truth.
"""
import hashlib
import json
import inspect
import math
import statistics
import time
from collections import Counter

from .parameter_grid import get, replay
from .pricing import price
from .recall import apply_cap

ALGORITHM = 'recorded-m-research-v1'
WINDOW = 20
MIN_TRAIN_GAMES = 5
SOURCES = ('recorded', 'fast', 'deep', 'focus', 'max', 'tier_median', 'tier_geomean', 'fast_deep_blend')
FACTORS = tuple(i / 100 for i in range(50, 151))


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def alternatives(rec, line, chosen):
    """Use comparable conditional estimates; unsupported alternatives fall back visibly."""
    amounts = {}
    for tier in ('fast', 'deep', 'focus', 'max'):
        est = get(rec.get('est', {}).get(tier, {}), line, {})
        m, p = est.get('m_raw', est.get('m')), est.get('p')
        if (positive(m) and isinstance(p, (int, float)) and math.isfinite(p)
                and not isinstance(p, bool) and 0 <= p <= 1
                and abs(p-chosen['p']) <= .1 + 1e-12
                and est.get('stated') is None and est.get('shared') is None
                and est.get('cap') == chosen.get('cap')):
            amounts[tier] = m
    # Focus uses the deep model: do not count both as independent ensemble members.
    independent, seen = [], set()
    for tier in ('fast', 'deep', 'max'):
        identity = rec.get('models', {}).get(tier, tier)
        if tier in amounts and identity not in seen:
            independent.append(amounts[tier])
            seen.add(identity)
    result = {'recorded': chosen['m'], **amounts}
    if len(independent) >= 2:
        result['tier_median'] = statistics.median(independent)
        result['tier_geomean'] = math.exp(statistics.fmean(math.log(m) for m in independent))
    if ('fast' in amounts and 'deep' in amounts
            and rec.get('models', {}).get('fast', 'fast') != rec.get('models', {}).get('deep', 'deep')):
        result['fast_deep_blend'] = (amounts['fast'] + amounts['deep']) / 2
    return result


def _summary(source, factor, games):
    lo = math.fsum(g['lower'] for g in games)
    hi = math.fsum(g['upper'] for g in games)
    actual = math.fsum(g['actual'] for g in games)
    deltas = [g['lower']-g['actual'] for g in games]
    return {'source': source, 'm_factor': factor, 'lower': round(lo, 2),
            'upper': round(hi, 2) if math.isfinite(hi) else None,
            'delta_lower': round(lo-actual, 2),
            'delta_upper': round(hi-actual, 2) if math.isfinite(hi) else None,
            'without_best_game_delta_lower': round(sum(deltas)-max(deltas, default=0), 2)}


def run(prepared, window, team, *, checkpoint=None, previous=None):
    import calibrate as C
    start = time.monotonic()
    window = sorted(set(window))[-WINDOW:]
    selected = sorted((e for e in prepared if e[0]['game'] in window), key=lambda e: e[0]['game'])
    if checkpoint:
        checkpoint()
    fingerprint = hashlib.sha256(json.dumps(
        {'algorithm': ALGORITHM, 'code': [inspect.getsource(fn) for fn in
            (run, alternatives, positive, _summary, replay, price, apply_cap, C.score_submission)], 'sources': SOURCES, 'factors': FACTORS, 'window': window,
         'minimum_train_games': MIN_TRAIN_GAMES, 'team': team, 'data': [(rec, data) for rec, data, _, _ in selected]},
        sort_keys=True, allow_nan=False).encode()).hexdigest()
    if (previous and previous.get('algorithm') == ALGORITHM
            and previous.get('fingerprint') == fingerprint):
        if checkpoint:
            checkpoint()
        return {**previous, 'reused': True}
    candidates = [(s, f) for s in SOURCES for f in FACTORS]
    scores = {c: [] for c in candidates}
    per_game, exclusions = [], []
    for rec, data, lines, final in selected:
        if checkpoint:
            checkpoint()
        totals = {c: [0., 0.] for c in candidates}
        actual, eligible = 0., 0
        available, fallback = Counter(), Counter()
        for line, inference in sorted(lines.items()):
            if checkpoint:
                checkpoint()
            tx = [r for r in data['tx'] if r['line_item_index'] == line]
            income = math.fsum(r['amount'] for r in tx if r['issuer'] == team)
            paid = math.fsum(r['amount'] for r in tx if r['reviewer'] == team)
            actual += income-paid
            est = get(rec.get('est', {}).get(get(rec['line_sources'], line), {}), line, {})
            try:
                if est.get('stated') is not None or est.get('shared') is not None:
                    raise ValueError('document_anchor_or_shared_allocation')
                alpha, sigma = est.get('a_mult', rec.get('a_mult')), est.get('sigma', rec.get('sigma'))
                original = replay(rec, line, alpha, sigma)
                if any(abs(a-b) > .001 for a, b in zip(original, final[line])):
                    raise ValueError('baseline_replay_mismatch')
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                exclusions.append({'game': rec['game'], 'line': line,
                                   'reason': str(exc) if isinstance(exc, ValueError) else 'missing_metadata'})
                for total in totals.values():
                    total[0] += income-paid
                    total[1] += income-paid
                continue
            eligible += 1
            choices = alternatives(rec, line, est)
            for source in SOURCES:
                (available if source in choices else fallback)[source] += 1
            recall = rec.get('recall', {})
            incoming, outgoing, prices = {}, {}, {}
            for index, (source, factor) in enumerate(candidates):
                if checkpoint and index % 64 == 0:
                    checkpoint()
                # An unavailable strategy keeps this line exactly as sent for EVERY
                # factor; it must not silently turn into a different estimator.
                if source not in choices:
                    totals[(source, factor)][0] += income-paid
                    totals[(source, factor)][1] += income-paid
                    continue
                m = choices[source] * factor
                if m not in prices:
                    ab = price(est['p'], m, None, est.get('cap'), 0, a_mult=alpha, sigma=sigma,
                               bias=est.get('bias', 1),
                               uncertainty=est.get('log_spread', 0) if est.get('uncertainty_applied') else 0)
                    prices[m] = apply_cap(ab, get(recall.get('lines', {}), line), dict(est),
                                          str(recall.get('mode', '0')))
                a, b = prices[m]
                if a not in incoming:
                    incoming[a] = ({'lower': income, 'upper': income} if a == final[line][0] else
                        C.score_submission({**inference, 'a_bounds': {}}, team, a, 0))
                if b not in outgoing:
                    outgoing[b] = ({'lower': -paid, 'upper': -paid} if b == final[line][1] else
                        C.score_submission({**inference, 'b': {}}, team, 0, b))
                for i, bound in enumerate(('lower', 'upper')):
                    totals[(source, factor)][i] += incoming[a][bound]+outgoing[b][bound]
        for candidate in candidates:
            scores[candidate].append({'game': rec['game'], 'lower': totals[candidate][0],
                                      'upper': totals[candidate][1], 'actual': actual})
        per_game.append({'game': rec['game'], 'replayed_lines': eligible,
                         'available': dict(available), 'unchanged_missing_source': dict(fallback)})

    rank = lambda row: (-row['lower'], abs(row['m_factor']-1), SOURCES.index(row['source']), row['m_factor'])
    ranking = sorted((_summary(*c, scores[c]) for c in candidates), key=rank) if selected else []
    source_counts = {s: sum(g['available'].get(s, 0) for g in per_game) for s in SOURCES}
    ranking = [row for row in ranking if source_counts[row['source']] > 0]
    for row in ranking:
        row['replayed_lines'] = source_counts[row['source']]
    usable = [i for i, g in enumerate(per_game) if g['replayed_lines'] > 0]
    # Evaluate all fixed candidates on the SAME later games, plus the adaptive
    # policy selected from the past at each step. Future best is never deployed.
    held_indices = usable[MIN_TRAIN_GAMES:]
    holdout_ranking = sorted((_summary(*c, [scores[c][i] for i in held_indices]) for c in candidates), key=rank) if held_indices else []
    steps = []
    for offset in range(MIN_TRAIN_GAMES, len(usable)):
        if checkpoint:
            checkpoint()
        earlier, target = usable[:offset], usable[offset]
        # A source must have observations on >=3 earlier games before selection.
        allowed = [c for c in candidates if sum(per_game[i]['available'].get(c[0], 0) > 0 for i in earlier) >= 3]
        best = min((_summary(*c, [scores[c][i] for i in earlier]) for c in allowed), key=rank)
        c = best['source'], best['m_factor']
        steps.append({'game': per_game[target]['game'], 'train_games': [per_game[i]['game'] for i in earlier],
                      'source_available_lines': per_game[target]['available'].get(c[0], 0),
                      **_summary(*c, [scores[c][target]])})
    for row in ranking[:10]:
        row['games'] = [{**g, 'upper': g['upper'] if math.isfinite(g['upper']) else None}
                        for g in scores[(row['source'], row['m_factor'])]]
    best = next((r for r in ranking if r['replayed_lines'] > 0), None)
    result = {'algorithm': ALGORITHM, 'fingerprint': fingerprint, 'reused': False,
              'status': 'research_only' if best else 'no_replayable_estimates', 'activation': 'none',
              'window_games': window, 'missing_games': sorted(set(window)-{g['game'] for g in per_game}),
              'candidate_count': len(candidates), 'replayed_lines': sum(g['replayed_lines'] for g in per_game),
              'source_counts': source_counts, 'per_game': per_game, 'exclusions': exclusions,
              'best_retrospective': best, 'ranking': ranking, 'fixed_candidate_test_scores': holdout_ranking,
              'walk_forward': {'games': steps, 'delta_lower': round(sum(g['delta_lower'] for g in steps), 2),
                  'delta_upper': (round(sum(g['delta_upper'] for g in steps), 2)
                                  if all(g['delta_upper'] is not None for g in steps) else None)},
              'elapsed_seconds': round(time.monotonic()-start, 3)}
    if checkpoint:
        checkpoint()
    return result


def report(state):
    if not state:
        return []
    lines = ['', '## Conditional-price estimator research (m; no automatic activation)', '',
             f"Games {state['window_games']}; missing {state['missing_games']}; "
             f"{state['candidate_count']} candidates; {state['replayed_lines']} replayable lines; "
             f"computed in {state['elapsed_seconds']}s; reused={state['reused']}.",
             'Same recorded p, A_MULT, SIGMA, bias, line caps and recall. Document/shared-cap lines stay unchanged.',
             'Missing/incompatible source estimates keep the actual submitted prices. Coverage differs by source:',
             str(state['source_counts']),
             'These are retrospective interval bounds, not precise forecasts. Repeated votes do not count as independent models.',
             '| Source | m factor | Net lower..upper | Delta lower..upper | Without best game delta lower |',
             '|---|---|---|---|---|']
    for row in state['ranking'][:10]:
        lines.append(f"| {row['source']} | {row['m_factor']} | {row['lower']}..{row['upper']} | "
                     f"{row['delta_lower']}..{row['delta_upper']} | {row['without_best_game_delta_lower']} |")
    forward = state['walk_forward']
    lines += ['', f"Chronological selection: {len(forward['games'])} later games; delta vs actual "
              f"{forward['delta_lower']}..{forward['delta_upper']} EUR. At least five earlier games select each pair.",
              'No future-game results enter selection; current model/prompt changes can limit transfer.']
    for row in forward['games']:
        lines.append(f"- Game {row['game']}: {row['source']} x {row['m_factor']}; "
                     f"delta {row['delta_lower']}..{row['delta_upper']}; available lines {row['source_available_lines']}")
    return lines
