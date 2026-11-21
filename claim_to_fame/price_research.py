"""Chronological validation of memory and category corrections, and guarded use.

The fixed policy tries comparable settlements first, a shrunk category fit second.
Every replay uses only older observations. Historical prices are hypotheses, not
hidden truths. Document limits, shared allocations and existing calibration take priority.
"""
import hashlib
import math
from pathlib import Path

from . import price_memory as PM
from .parameter_grid import get, replay
from .pricing import price
from .recall import apply_cap

VERSION = 'price-evidence-v1'
MIN_GAMES, MIN_LINES, MIN_GAIN = 3, 8, .05
IDENTITY = ('tier', 'model', 'prompt_hash', 'config_hash')


def signature():
    return hashlib.sha256((PM.identity() + Path(__file__).read_text()).encode()).hexdigest()


def identity(rec, tier):
    def field(name):
        value = rec.get(name)
        return value.get(tier) if isinstance(value, dict) else value
    return {'tier': tier, 'model': rec.get('models', {}).get(tier),
            'prompt_hash': field('prompt_hash'), 'config_hash': field('config_hash')}


def scope(est):
    if any(est.get(k) is not None for k in ('stated', 'cap', 'shared')):
        return False
    return (PM.positive(est.get('m')) and type(est.get('p')) in (float, int)
            and .9 <= est['p'] <= 1 and not est.get('calibration_applied'))


def category_fit(observations, category, who):
    rows = [r for r in observations if r['category'] == category and r['identity'] == who]
    if category in ('unknown', 'other') or len(rows) < 8 or len({r['game'] for r in rows}) < 3:
        return None
    # Minimise distance to intervals, never to invented midpoint labels. Weak
    # ridge plus shrinkage prevents small or wide datasets prescribing extremes.
    def loss(factor):
        value = math.log(factor)
        residual = 0.
        for row in rows:
            lo, hi = row['ratio']
            distance = max(math.log(lo) - value, 0, value - math.log(hi) if hi else 0)
            residual += distance * distance
        return residual + 4 * value * value
    factor = min((i / 100 for i in range(75, 126)), key=lambda f: (loss(f), abs(f-1)))
    factor = math.exp(math.log(factor) * len(rows) / (len(rows) + 12))
    return {'factor': factor, 'source_games': sorted({r['game'] for r in rows}),
            'lines': len(rows), 'reason': 'category_interval_fit', 'category': category}


def propose(est, hits, observations, who, category_models=None):
    if not scope(est):
        return None
    m = est.get('m_before_price_learning', est['m'])
    suggestion = PM.suggest(m, hits)
    if suggestion:
        return suggestion
    category = est.get('valuation_category', 'unknown')
    key = (*who.values(), category)
    if category_models is not None:
        if key not in category_models:
            category_models[key] = category_fit(observations, category, who)
        fitted = category_models[key]
    else:
        fitted = category_fit(observations, category, who)
    if fitted and abs(fitted['factor']-1) >= .005:
        return {**fitted, 'm': m * fitted['factor']}
    return None


def observation(rec, line, est, bounds, who):
    m = est.get('m_before_price_learning', est.get('m_raw', est.get('m')))
    if not scope(est) or not PM.positive(m) or bounds[0] <= 0:
        return None
    return {'game': rec['game'], 'line': line, 'identity': who,
            'category': est.get('valuation_category', 'unknown'),
            'ratio': [bounds[0] / m, bounds[1] / m if math.isfinite(bounds[1]) else None]}


def pair_for(rec, line, est, m):
    pair = price(est['p'], m, None, None, 0,
                 a_mult=est.get('a_mult', rec['a_mult']), sigma=est.get('sigma', rec['sigma']),
                 bias=est.get('bias', 1),
                 uncertainty=est.get('log_spread', 0) if est.get('uncertainty_applied') else 0)
    recall = rec.get('recall', {})
    return apply_cap(pair, get(recall.get('lines', {}), line), {}, str(recall.get('mode', '0')))


def bounds_for(pair, inference, tx, final, team):
    import calibrate as C
    a, b = pair
    income = math.fsum(t['amount'] for t in tx if t['issuer'] == team)
    paid = math.fsum(t['amount'] for t in tx if t['reviewer'] == team)
    inc = {'lower': income, 'upper': income} if a == final[0] else C.score_submission(
        {**inference, 'a_bounds': {}}, team, a, 0)
    out = {'lower': -paid, 'upper': -paid} if b == final[1] else C.score_submission(
        {**inference, 'b': {}}, team, 0, b)
    return inc['lower'] + out['lower'], inc['upper'] + out['upper']


def promotion(rows):
    rows = [r for r in rows if r['changed']]
    games = sorted({r['game'] for r in rows})
    finite = bool(rows) and all(r['delta_lower'] is not None for r in rows)
    by_game = [sum(r['delta_lower'] for r in rows if r['game'] == g) for g in games] if finite else []
    gain = sum(by_game) if finite else None
    denominator = max(1., sum(abs(r['baseline_lower']) for r in rows))
    without_best = gain - max(by_game) if finite else None
    validated = (len(games) >= MIN_GAMES and len(rows) >= MIN_LINES and finite
                 and gain >= MIN_GAIN * denominator and without_best > 0)
    return dict(status='validated' if validated else 'shadow', changed_lines=len(rows),
                test_games=games, delta_lower=gain, without_best_game=without_best,
                conservative_gain=gain/denominator if finite else None)


def evaluate(prepared, team, *, checkpoint=None):
    memory, observations, cohorts, exclusions = [], [], {}, []
    for rec, data, lines, final in sorted(prepared, key=lambda x: x[0]['game']):
        if checkpoint:
            checkpoint()
        hits = PM.retrieve(memory, rec['items'], rec.get('policy_hash'),
                           before_game=rec['game'], checkpoint=checkpoint)
        pending_memory, pending_obs, category_models = [], [], {}
        for line, inference in sorted(lines.items()):
            if checkpoint:
                checkpoint()
            tier = get(rec['line_sources'], line)
            est = get(rec.get('est', {}).get(tier, {}), line, {})
            who = identity(rec, tier)
            item = get(rec['items'], line, {})
            entry = PM.make_entry(rec, line, item, est, inference['t'], f"game_{rec['game']:03d}.json")
            if entry:
                pending_memory.append(entry)
            if not all(isinstance(v, str) and v for v in who.values()):
                continue
            obs = observation(rec, line, est, inference['t'], who)
            if obs:
                pending_obs.append(obs)
            suggestion = propose(est, hits.get(str(line), []), observations, who, category_models)
            if not suggestion:
                continue
            try:
                actual = replay(rec, line, est.get('a_mult', rec['a_mult']), est.get('sigma', rec['sigma']))
                if any(abs(a-b) > .001 for a, b in zip(actual, final[line])):
                    raise ValueError('baseline_replay_mismatch')
                base_m = est.get('m_before_price_learning', est['m'])
                candidate_pair = pair_for(rec, line, est, suggestion['m'])
                baseline_pair = pair_for(rec, line, est, base_m)
                tx = [t for t in data['tx'] if t['line_item_index'] == line]
                base_lo, base_hi = bounds_for(baseline_pair, inference, tx, final[line], team)
                cand_lo, cand_hi = bounds_for(candidate_pair, inference, tx, final[line], team)
                row = {'game': rec['game'], 'line': line, 'reason': suggestion['reason'],
                       'm_before': base_m, 'm_after': suggestion['m'],
                       'source_games': suggestion['source_games'],
                       'baseline_lower': base_lo, 'baseline_upper': base_hi if math.isfinite(base_hi) else None,
                       'delta_lower': cand_lo-base_hi if math.isfinite(base_hi) else None,
                       'delta_upper': cand_hi-base_lo if math.isfinite(cand_hi) else None,
                       'changed': candidate_pair != baseline_pair}
                channel, category = suggestion['reason'], suggestion.get('category')
                key = (*[who[k] for k in IDENTITY], channel, category)
                cohorts.setdefault(key, {'identity': who, 'channel': channel, 'category': category,
                                         'rows': []})['rows'].append(row)
            except (KeyError, TypeError, ValueError, OverflowError):
                exclusions.append({'game': rec['game'], 'line': line, 'reason': 'unreplayable'})
        # Same game's other lines may not train this game's predictions.
        memory.extend(pending_memory)
        observations.extend(pending_obs)
    for candidate in cohorts.values():
        candidate.update(promotion(candidate['rows']))
    return {'version': VERSION, 'signature': signature(), 'cohorts': list(cohorts.values()),
            'observations': observations[-5000:], 'exclusions': exclusions,
            'method': 'fixed memory-then-category policy; each game uses earlier games only',
            'minimum_games': MIN_GAMES, 'minimum_lines': MIN_LINES, 'minimum_gain': MIN_GAIN}


def valid(research, games):
    """Reject incomplete or inconsistent research provenance at artifact load."""
    try:
        if research.get('version') != VERSION or not isinstance(research['signature'], str):
            return False
        seen = set()
        for c in research['cohorts']:
            key = (*[c['identity'][k] for k in IDENTITY], c['channel'], c['category'])
            if key in seen or not all(isinstance(c['identity'][k], str) and c['identity'][k] for k in IDENTITY):
                return False
            seen.add(key)
            if c['channel'] not in ('comparable_settlements', 'category_interval_fit'):
                return False
            row_ids = set()
            for r in c['rows']:
                if (r['game'] not in games or (r['game'], r['line']) in row_ids
                        or not r['source_games'] or any(g not in games or g >= r['game'] for g in r['source_games'])
                        or not PM.positive(r['m_before']) or not PM.positive(r['m_after'])
                        or type(r['changed']) is not bool):
                    return False
                row_ids.add((r['game'], r['line']))
                if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v))
                       for v in (r['baseline_lower'], r['baseline_upper'], r['delta_lower'], r['delta_upper'])):
                    return False
            if any(c.get(k) != v for k, v in promotion(c['rows']).items()):
                return False
        for o in research['observations']:
            lo, hi = o['ratio']
            if (o['game'] not in games or not PM.positive(lo)
                    or (hi is not None and (not PM.positive(hi) or hi <= lo))
                    or not all(isinstance(o['identity'][k], str) and o['identity'][k] for k in IDENTITY)):
                return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def adjust(state, item, est, who, policy_hash, game, *, mode='validated', category_models=None):
    diagnostic = {'applied': False, 'reason': 'no_validated_evidence'}
    if mode == 'off' or not state or not scope(est):
        return est['m'], diagnostic
    research = state.get('price_research', {})
    if research.get('signature') != signature() or state.get('max_game', game) >= game:
        return est['m'], diagnostic
    hits = PM.retrieve(state.get('memory', []), {'current': item}, policy_hash, before_game=game)
    observations = [o for o in research.get('observations', []) if o['game'] < game]
    suggestion = propose(est, hits.get('current', []), observations, who, category_models)
    if not suggestion:
        return est['m'], diagnostic
    validated = any(c['identity'] == who and c['status'] == 'validated'
                    and c.get('channel') == suggestion['reason'] and c.get('category') == suggestion.get('category')
                    and c['test_games'] and max(c['test_games']) < game for c in research.get('cohorts', []))
    applied = validated and mode == 'validated'
    diagnostic.update(suggestion, applied=applied, validated=validated,
                      reason=suggestion['reason'], evidence_max_game=state['max_game'])
    return suggestion['m'] if applied else est['m'], diagnostic


def report(research):
    if not research:
        return []
    lines = ['Comparable-item and category price learning (chronological):']
    for c in research.get('cohorts', []):
        lines.append(f"- {c['identity']['tier']} / {c['channel']} / {c['category']}: {c['status']}; {c['changed_lines']} changed lines, "
                     f"games {c['test_games']}; conservative delta {c['delta_lower']}; "
                     f"without best game {c['without_best_game']}")
    return lines
