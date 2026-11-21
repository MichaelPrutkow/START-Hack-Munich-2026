"""Auditable arithmetic and explicit cross-stage selection; not a truth oracle."""
import math

CATEGORIES = ('equipment', 'labour', 'material', 'accessory', 'ancillary', 'other')
MAX_UNREVIEWED_RATIO = 1.15


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid valuation number')
    return float(value)


def audit(vote):
    value = vote.get('valuation')
    if value is None:
        return {'status': 'missing', 'category': 'unknown'}
    try:
        if value['category'] not in CATEGORIES:
            raise ValueError('unknown category')
        components = value['components']
        if not isinstance(components, list) or not 1 <= len(components) <= 12:
            raise ValueError('expected 1..12 components')
        total = math.fsum(number(c['quantity']) * number(c['unit_gross']) for c in components)
        share = number(value['covered_share'])
        if share > 1 or not isinstance(value['share_basis'], str) or not value['share_basis'].strip():
            raise ValueError('invalid share or missing basis')
        if not isinstance(value['coverage_basis'], str) or not value['coverage_basis'].strip():
            raise ValueError('missing coverage basis')
        expected = total * share
        if vote.get('stated_cap') is not None:
            expected = min(expected, number(vote['stated_cap']))
        target = number(vote['t_if_covered'])
        if not math.isfinite(expected) or abs(expected-target) > max(.02, .005*target):
            raise ValueError('components/share do not reproduce conditional total')
        return {'status': 'consistent', 'category': value['category'], 'base_total': total,
                'covered_share': share, 'conditional_total': expected}
    except (KeyError, ValueError, TypeError, OverflowError):
        return {'status': 'inconsistent', 'category': 'unknown'}


def disagreement(candidate, previous):
    if not previous:
        return 1., 0.
    a, b = candidate.get('m_raw', candidate.get('m', 0)), previous.get('m_raw', previous.get('m', 0))
    ratio = max(a, b) / min(a, b) if a > 0 and b > 0 else 1.
    return ratio, abs(candidate.get('p', 0)-previous.get('p', 0))


def selection(candidate, previous, *, level, previous_level, new_quality, old_quality):
    if not candidate.get('valid_count', 0):
        return False, 'no_valid_votes'
    if previous is None:
        return True, 'first_usable_estimate'
    ratio, _ = disagreement(candidate, previous)
    reviewed = candidate.get('independent_recheck') and candidate.get('valuation_consistent', False)
    if ratio > MAX_UNREVIEWED_RATIO and not reviewed:
        return False, 'large_price_change_needs_independent_breakdown'
    # Agreeing repetitions are correlated; the quality function gives no sample-count bonus.
    if level > previous_level and new_quality <= old_quality + .15:
        return True, 'independent_breakdown' if reviewed else 'comparable_consistency'
    if new_quality + .15 < old_quality:
        return True, 'better_consistency'
    return False, 'retain_confirmed_estimate'
