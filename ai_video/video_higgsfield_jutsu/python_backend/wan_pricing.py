"""Wan Standard list-price estimates in USD, based on generated output seconds.

This application formula intentionally excludes input-video seconds, discounts,
taxes and auxiliary AI/image calls. It is not a provider invoice reconciliation.
"""

from decimal import Decimal
import math


WAN_USD_PER_SECOND = {
    'wan3.0-video': {'480P': Decimal('0.05'), '720P': Decimal('0.10'), '1080P': Decimal('0.20')},
}


def _positive_seconds(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def price_wan_usage(row, *, resolution, duration):
    """Enrich a completed Wan receipt without mutating provider metering."""
    if not isinstance(row, dict) or row.get('provider') != 'dashscope':
        return row
    rate = WAN_USD_PER_SECOND.get(row.get('model'), {}).get(str(resolution).upper())
    if rate is None:
        return row
    if row.get('call_status') in {'failed', 'canceled', 'unknown'}:
        return {**row, 'credits_charged': 0}
    if row.get('call_status') not in {'succeeded', 'completed', 'success'}:
        return row
    seconds = row.get('duration_seconds')
    source = 'provider_output'
    if not _positive_seconds(seconds):
        seconds, source = duration, 'request'
    if not _positive_seconds(seconds):
        return row
    amount = float(rate * Decimal(str(seconds)))
    pricing = {'formula': 'wan-output-duration-v1', 'currency': 'USD',
               'resolution': str(resolution).upper(), 'usd_per_second': float(rate),
               'duration_seconds': seconds, 'duration_source': source,
               'amount': amount}
    return {**row, 'credits_charged': amount,
            'raw_usage': {**(row.get('raw_usage') or {}), 'pricing': pricing}}
