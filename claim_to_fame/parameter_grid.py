"""Retrospective fixed-opponent replay. Never supplies live parameters."""
import math
from collections import Counter
from .pricing import price
from .recall import apply_cap

WINDOW_SIZE = 20

ALPHAS = tuple(round(.5 + i * .05, 2) for i in range(13))
SIGMAS = (.2, .3, .35, .5, .65, .8, 1., 1.2, 1.4)


def get(mapping, key, default=None):
    return mapping.get(str(key), mapping.get(key, default))


def replay(rec, line, alpha, sigma):
    source = get(rec['line_sources'], line)
    est = get(rec.get('est', {}).get(source, {}), line, {})
    if source == 'rules' or not est or est.get('m', 0) <= 0:
        raise ValueError('missing_original_estimate_or_rules')
    recall = rec.get('recall', {})
    if not recall and any(est.get(k) for k in ('recall_applied', 'recall_b_applied')):
        raise ValueError('missing_historical_recall')
    pair = price(est['p'], est['m'], est.get('stated'), est.get('cap'), 0,
                 a_mult=alpha, sigma=sigma, bias=est.get('bias', 1),
                 uncertainty=est.get('log_spread', 0) if est.get('uncertainty_applied') else 0)
    return apply_cap(pair, get(recall.get('lines', {}), line), dict(est), str(recall.get('mode', '0')))


def evaluate(prepared, window, team, checkpoint=None):
    """prepared contains only fully reconciled games; window also includes missing logs."""
    import calibrate as C
    window = sorted(set(window))[-WINDOW_SIZE:]
    selected = [entry for entry in prepared if entry[0]['game'] in window]
    combos = {(a, s) for a in ALPHAS for s in SIGMAS}
    for rec, _, _, _ in selected:
        for source in rec.get('est', {}).values():
            for est in source.values():
                a, s = est.get('a_mult', rec.get('a_mult')), est.get('sigma', rec.get('sigma'))
                if all(isinstance(x, (int, float)) and math.isfinite(x) and x >= 0 for x in (a, s)):
                    combos.add((a, s))
    scores = {pair: [] for pair in sorted(combos)}
    exclusions, game_info = [], []
    for rec, data, lines, final in selected:
        if checkpoint:
            checkpoint()
        gid = rec['game']
        baseline, eligible, fixed = 0., 0, Counter()
        totals = {pair: [0., 0.] for pair in scores}
        for line, inference in sorted(lines.items()):
            tx = [t for t in data['tx'] if t['line_item_index'] == line]
            income = math.fsum(t['amount'] for t in tx if t['issuer'] == team)
            paid = math.fsum(t['amount'] for t in tx if t['reviewer'] == team)
            actual = income - paid
            baseline += actual
            est = get(rec.get('est', {}).get(get(rec['line_sources'], line), {}), line, {})
            try:
                original = replay(rec, line, est.get('a_mult', rec.get('a_mult')),
                                  est.get('sigma', rec.get('sigma')))
                if any(abs(a-b) > .001 for a, b in zip(original, final[line])):
                    raise ValueError('baseline_replay_mismatch')
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else 'missing_pricing_metadata'
                fixed[reason] += 1
                exclusions.append({'game': gid, 'line': line, 'reason': reason})
                for total in totals.values():
                    total[0] += actual
                    total[1] += actual
                continue
            eligible += 1
            # Role scores are separable; cache at most 13 + 9 distinct prices per line.
            incoming, outgoing = {}, {}
            for pair, total in totals.items():
                if checkpoint:
                    checkpoint()
                a, b = replay(rec, line, *pair)
                if a not in incoming:
                    incoming[a] = ({'lower': income, 'upper': income} if a == final[line][0] else
                                   C.score_submission({**inference, 'a_bounds': {}}, team, a, 0))
                if b not in outgoing:
                    outgoing[b] = ({'lower': -paid, 'upper': -paid} if b == final[line][1] else
                                   C.score_submission({**inference, 'b': {}}, team, 0, b))
                for i, bound in enumerate(('lower', 'upper')):
                    total[i] += incoming[a][bound] + outgoing[b][bound]
        for pair, total in totals.items():
            scores[pair].append({'game': gid, 'lower': total[0], 'upper': total[1],
                                 'actual': baseline})
        game_info.append({'game': gid, 'replayed_lines': eligible, 'fixed_lines': sum(fixed.values()),
                          'fixed_reasons': dict(fixed), 'actual': round(baseline, 2),
                          'commit': rec.get('commit', 'unknown')})

    def result(pair, rows):
        lo, hi = (math.fsum(r[k] for r in rows) for k in ('lower', 'upper'))
        actual = math.fsum(r['actual'] for r in rows)
        deltas = [r['lower'] - r['actual'] for r in rows]
        return {'a_mult': pair[0], 'sigma': pair[1], 'lower': round(lo, 2),
                'upper': round(hi, 2) if math.isfinite(hi) else None,
                'delta_lower': round(lo-actual, 2),
                'delta_upper': round(hi-actual, 2) if math.isfinite(hi) else None,
                'without_best_game_delta_lower': round(sum(deltas)-max(deltas, default=0), 2)}

    ranked = sorted((result(pair, rows) for pair, rows in scores.items()),
                    key=lambda r: (-r['lower'], r['a_mult'], r['sigma'])) if selected else []
    for row in ranked:
        row['games'] = [{**g, 'lower': round(g['lower'], 2),
                         'upper': round(g['upper'], 2) if math.isfinite(g['upper']) else None}
                        for g in scores[(row['a_mult'], row['sigma'])]]
    usable = any(g['replayed_lines'] for g in game_info)
    per_game = []
    for i, game in enumerate(game_info):
        best = min((result(pair, [rows[i]]) for pair, rows in scores.items()),
                   key=lambda r: (-r['lower'], r['a_mult'], r['sigma']))
        per_game.append({**game, 'best': best if game['replayed_lines'] else None})
    return {'status': 'retrospective_only' if usable else 'no_replayable_estimates',
            'window_size': WINDOW_SIZE, 'window_games': window, 'missing_games': sorted(set(window)-{g['game'] for g in game_info}),
            'replayed_lines': sum(g['replayed_lines'] for g in game_info),
            'fixed_lines': sum(g['fixed_lines'] for g in game_info),
            'combination_count': len(scores), 'best': ranked[0] if usable else None,
            'ranking': ranked, 'per_game': per_game, 'exclusions': exclusions}


def report(grid):
    if not grid:
        return []
    lines = ['', f"## Retrospective A_MULT / SIGMA grid (last {grid.get('window_size', 10)} completed cached games)", '',
             f"Games: {grid['window_games']}; missing logs/reconciliation: {grid['missing_games']}; "
             f"replayed lines: {grid['replayed_lines']}; unchanged lines: {grid['fixed_lines']}.",
             'Original estimates, coverage, bias, policy caps and historical recall stay fixed. Opponents stay fixed.',
             'Ranked by conservative net lower bound. Overlapping intervals do not prove a winner; ties use smaller A_MULT then SIGMA.',
             'This is hindsight selection, not independent validation or automatic activation. Model/prompt changes may mix strategies.',
             'All combinations are stored in learning.json under parameter_grid.ranking; no new model calls.', '',
             '| A_MULT | SIGMA | Net EUR lower..upper | Delta vs actual lower..upper | Delta lower without best game |',
             '|---|---|---|---|---|']
    if grid['best']:
        for row in grid['ranking'][:10]:
            lines.append(f"| {row['a_mult']} | {row['sigma']} | {row['lower']}..{row['upper']} | "
                         f"{row['delta_lower']}..{row['delta_upper']} | {row['without_best_game_delta_lower']} |")
    for game in grid['per_game']:
        lines.append(f"- Game {game['game']}: actual {game['actual']}; best conservative combination: {game['best']}; "
                     f"fixed lines: {game['fixed_reasons']}")
    return lines
