#!/usr/bin/env python3
"""Bounded paired reassessment of completed LOCAL cases; never submits or decrypts.

Default is a plan with no API calls. --run uses the configured model only between
live windows. Frozen original p, caps, parameters and opponents isolate price effects.
Output is private research; it cannot automatically replace the production prompt.
"""
import argparse
import hashlib
import json
import math
import time
from pathlib import Path

from claim_to_fame import feedback as F, price_memory as M, price_research as P, prompts, pricing
from claim_to_fame.parameter_grid import get, replay

VARIANTS = ('components', 'comparables')


def messages(policy, description, items, memory, gid, variant):
    matches = M.retrieve(memory, items, M.hash_policy(policy), before_game=gid)
    context = M.context(matches) if variant == 'comparables' else ''
    result = prompts.build_prompt(policy, description, items, historical_context=context)
    if variant == 'components':
        result[1]['content'] += ('\nPRICE EXPERIMENT: independently check the ordinary replacement/repair '
                                'cost of each component. Distinguish hours, materials and call-out fees. '
                                'Do not duplicate costs across lines. Explain the main uncertain unit rate '
                                'and check that the component totals reproduce your estimate.\n')
    return result


def experiment(rec, data, lines, final, policy, description, memory, provider, guard, *, team, holdout_from):
    """provider(messages) -> parsed votes; injected for completely offline tests."""
    output = []
    for variant in VARIANTS:
        try:
            guard()
        except TimeoutError:
            break
        prompt = messages(policy, description, rec['items'], memory, rec['game'], variant)
        started = time.monotonic()
        try:
            votes, usage = provider(prompt)
            row = {'game': rec['game'], 'variant': variant,
                   'split': 'holdout' if rec['game'] >= holdout_from else 'train',
                   'prompt_sha256': hashlib.sha256(json.dumps(prompt, sort_keys=True).encode()).hexdigest(),
                   'seconds': time.monotonic()-started, 'usage': usage, 'lines': [], 'status': 'completed'}
            lo, hi, changed = 0., 0., 0
            for line, inference in lines.items():
                source = get(rec['line_sources'], line)
                est = get(rec.get('est', {}).get(source, {}), line, {})
                tx = [t for t in data['tx'] if t['line_item_index'] == line]
                actual = sum(t['amount'] for t in tx if t['issuer'] == team) - sum(t['amount'] for t in tx if t['reviewer'] == team)
                pair = final[line]
                proposal = pricing.estimate_votes([get(v, line) for v in votes if get(v, line) is not None])
                reason = 'unchanged_incompatible_or_missing_estimate'
                if P.scope(est) and proposal['valid_count'] and abs(proposal['p']-est['p']) <= .1:
                    try:
                        original = replay(rec, line, est.get('a_mult', rec['a_mult']), est.get('sigma', rec['sigma']))
                        if any(abs(a-b) > .001 for a, b in zip(original, final[line])):
                            raise ValueError('baseline mismatch')
                        if any(proposal.get(k) is not None for k in ('stated', 'cap', 'shared')):
                            raise ValueError('different cap semantics')
                        pair = P.pair_for(rec, line, est, proposal['m'])
                        reason = 'price_only_reassessment'
                        changed += 1
                    except (ValueError, TypeError, KeyError):
                        pass
                lower, upper = P.bounds_for(pair, inference, tx, final[line], team)
                lo += lower-actual
                hi += upper-actual
                row['lines'].append({'line': line, 'm': proposal['m'] if proposal['valid_count'] else None,
                                     'a_b': pair, 'reason': reason})
            row.update(delta_lower=lo, delta_upper=hi if math.isfinite(hi) else None, usable_lines=changed)
            output.append(row)
        except Exception as exc:
            output.append({'game': rec['game'], 'variant': variant, 'status': 'failed',
                           'reason': type(exc).__name__, 'split': 'holdout' if rec['game'] >= holdout_from else 'train'})
    return output


def summarize(rows):
    result = []
    for split in ('train', 'holdout'):
        for variant in VARIANTS:
            paired = {r['game'] for r in rows if r['split'] == split and r['status'] == 'completed'
                      and r.get('usable_lines', 0) > 0}
            paired = {g for g in paired if all(any(r['game'] == g and r['variant'] == v
                      and r['status'] == 'completed' and r.get('usable_lines', 0) > 0 for r in rows) for v in VARIANTS)}
            selected = [r for r in rows if r['game'] in paired and r['variant'] == variant]
            result.append({'split': split, 'variant': variant, 'paired_games': sorted(paired),
                           'delta_lower': sum(r['delta_lower'] for r in selected),
                           'delta_upper': sum(r['delta_upper'] for r in selected)
                           if all(r['delta_upper'] is not None for r in selected) else None})
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rounds', type=Path, default=Path('out/rounds.jsonl'))
    ap.add_argument('--cache', type=Path, default=Path('out/leaderboard'))
    ap.add_argument('--cases', type=Path, default=Path('out/cases'))
    ap.add_argument('--games', type=int, nargs='+', required=True)
    ap.add_argument('--holdout-from', type=int, required=True, help='choose split before running; do not tune on held-out games')
    ap.add_argument('--team', default='chineseModels')
    ap.add_argument('--tier', default='fast', choices=('fast', 'deep'))
    ap.add_argument('--max-calls', type=int, default=12)
    ap.add_argument('--budget-seconds', type=float, default=180.)
    ap.add_argument('--output', type=Path, default=Path('out/price-experiments.json'))
    ap.add_argument('--run', action='store_true', help='perform paid model calls; default prints a plan only')
    args = ap.parse_args(argv)
    games = sorted(set(args.games))
    if (not games or games[0] <= 0 or len(games)*len(VARIANTS) > args.max_calls or args.max_calls <= 0
            or not math.isfinite(args.budget_seconds) or not 0 < args.budget_seconds <= 600
            or not games[0] < args.holdout_from <= games[-1]):
        ap.error('require positive games, separate training/holdout games, sufficient call budget and <=600 seconds')
    if not args.run:
        print(json.dumps({'games': games, 'variants': VARIANTS, 'holdout_from': args.holdout_from,
                          'calls': len(games)*len(VARIANTS), 'mode': 'plan_only'}))
        return
    # Imports and credentials only after explicit execution. No play_game/submit.
    import runner as R
    from claim_to_fame.learning_service import _records, _safe_until
    records = _records(args.rounds)
    prepared, memory = {}, []
    for gid, rec in sorted(records.items()):
        if gid > max(games):
            continue
        try:
            data = F._strict_json((args.cache / f'game_{gid:03d}.json').read_text())
            lines, final = F._reconcile(rec, data, args.team)
            prepared[gid] = (rec, data, lines, final)
            for line, inf in lines.items():
                est = get(rec.get('est', {}).get(get(rec['line_sources'], line), {}), line, {})
                entry = M.make_entry(rec, line, get(rec['items'], line), est, inf['t'], 'cache')
                if entry:
                    memory.append(entry)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if any(g not in prepared or not (args.cases / f'case_{g:03d}').is_dir() for g in games):
        ap.error('every requested game needs a reconciled local settlement and already decrypted local case')
    R.load_env()
    R.POOL = R.KeyPool()
    tier = next((t for t in R.build_tiers() if t.name == args.tier), None)
    if tier is None:
        ap.error('requested model tier is disabled')
    tier.votes, tier.n_ok = 1, True
    schedule = R.list_games()
    starts = dict(schedule)
    if any(g not in starts or starts[g] + 65 >= time.time() for g in games):
        ap.error('requested games must be confirmed finished in the current schedule')
    end = time.time() + args.budget_seconds
    def guard():
        remaining = min(end, _safe_until(schedule, time.time(), margin=25)) - time.time()
        if remaining <= 6:
            raise TimeoutError('not enough idle time before next live window')
        return remaining
    def provider(msgs):
        budget = min(20., guard()-3)
        R.POOL.new_round()
        return R.assess(msgs, budget, tier)
    rows = []
    try:
        for gid in games:
            rec, data, lines, final = prepared[gid]
            directory = args.cases / f'case_{gid:03d}'
            rows += experiment(rec, data, lines, final, R.read_text(directory, 'policy*.txt'),
                               R.read_text(directory, 'description*.txt'), memory, provider, guard,
                               team=args.team, holdout_from=args.holdout_from)
            F._atomic_write(args.output, json.dumps({'model': tier.model, 'rows': rows, 'summary': summarize(rows),
                                                    'activation': 'none'}, ensure_ascii=False, allow_nan=False))
            guard()
    except TimeoutError:
        print('Stopped at the idle-time budget; completed results preserved.')
    print(json.dumps(summarize(rows)))


if __name__ == '__main__':
    main()
