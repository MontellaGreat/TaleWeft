"""Durable model billing with atomic budget reservations in the existing writer."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
import json
import uuid

from infrastructure.persistence.database.connection import get_database

SCALE = Decimal(1_000_000_000)


def money_nanos(value) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError('Amount must be finite and non-negative')
    nanos = int((amount * SCALE).to_integral_value(rounding=ROUND_CEILING))
    if nanos > 9_000_000_000_000_000_000:
        raise ValueError('Amount is too large')
    return nanos


def money_text(value: int) -> str:
    return format(Decimal(value) / SCALE, 'f')


def cost_nanos(input_tokens, output_tokens, input_price, output_price) -> int | None:
    if input_price is None or output_price is None:
        return None
    amount = (Decimal(str(input_price)) * input_tokens + Decimal(str(output_price)) * output_tokens) / 1_000_000
    return money_nanos(amount)


class BudgetExceededError(ValueError):
    pass


class UsageLedger:
    def __init__(self, db=None):
        self.db = db or get_database()
        self.db.execute('''CREATE TABLE IF NOT EXISTS ai_usage_ledger (
            id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
            profile_id TEXT NOT NULL, model TEXT NOT NULL, protocol TEXT NOT NULL,
            status TEXT NOT NULL, currency TEXT NOT NULL, input_price TEXT, output_price TEXT,
            input_tokens INTEGER, output_tokens INTEGER, actual_nanos INTEGER, exposure_nanos INTEGER,
            effective_max_tokens INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS ai_usage_budgets (
            novel_id TEXT PRIMARY KEY, currency TEXT NOT NULL, limit_nanos INTEGER,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS ai_usage_reconciliations (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            currency TEXT NOT NULL, cost_nanos INTEGER NOT NULL, reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()

    def set_budget(self, novel_id: str, *, currency: str, limit=None):
        if not novel_id.strip():
            raise ValueError('novel_id is required')
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha() or not currency.isascii():
            raise ValueError('currency must be a three-letter currency code')
        nanos = None if limit is None else money_nanos(limit)
        self.db.execute('''INSERT INTO ai_usage_budgets(novel_id, currency, limit_nanos)
            VALUES (?, ?, ?) ON CONFLICT(novel_id) DO UPDATE SET currency=excluded.currency,
            limit_nanos=excluded.limit_nanos, updated_at=CURRENT_TIMESTAMP''', (novel_id, currency, nanos))
        self.db.commit()
        return self.summary(novel_id)

    def reserve(self, *, novel_id: str, attempt_id: str, settings, model: str, input_bound: int, max_tokens: int) -> str:
        request_id = str(uuid.uuid4())
        currency = getattr(settings, 'currency', 'USD')
        input_price = getattr(settings, 'input_price_per_million', None)
        output_price = getattr(settings, 'output_price_per_million', None)
        exposure = cost_nanos(input_bound, max_tokens, input_price, output_price)
        # Conditional INSERT runs on the serialized writer as one statement.
        # A missing price/usage cannot silently consume zero budget. Different
        # currencies require a separate explicitly configured book policy.
        cursor = self.db.execute('''INSERT INTO ai_usage_ledger (
            id, novel_id, attempt_id, profile_id, model, protocol, status, currency,
            input_price, output_price, exposure_nanos, effective_max_tokens)
            SELECT ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM ai_usage_budgets b WHERE b.novel_id=?
                AND b.limit_nanos IS NOT NULL AND (
                b.currency<>? OR ? IS NULL OR
                EXISTS (SELECT 1 FROM ai_usage_ledger l WHERE l.novel_id=b.novel_id
                    AND (l.currency<>b.currency OR l.exposure_nanos IS NULL)) OR
                COALESCE((SELECT SUM(l.exposure_nanos) FROM ai_usage_ledger l
                    WHERE l.novel_id=b.novel_id), 0) + ? > b.limit_nanos))''',
            (request_id, novel_id, attempt_id, getattr(settings, 'profile_id', '') or '', model,
             getattr(settings, 'protocol', '') or '', currency,
             None if input_price is None else str(input_price), None if output_price is None else str(output_price),
             exposure, max_tokens, novel_id, currency, exposure, exposure))
        self.db.commit()
        if cursor.rowcount != 1:
            raise BudgetExceededError('小说模型预算不足，或既有调用金额未知 / 币种不一致；请核对账单、定价与预算设置')
        return request_id

    def finish(self, request_id: str, *, usage, status: str):
        row = self.db.fetch_one('SELECT * FROM ai_usage_ledger WHERE id=?', (request_id,))
        if row is None:
            raise ValueError('Unknown usage request')
        actual = None if usage is None else cost_nanos(usage.input_tokens, usage.output_tokens, row['input_price'], row['output_price'])
        self.db.execute('''UPDATE ai_usage_ledger SET status=?, input_tokens=?, output_tokens=?,
            actual_nanos=?, exposure_nanos=COALESCE(?, exposure_nanos), updated_at=CURRENT_TIMESTAMP WHERE id=?''',
            (status, None if usage is None else usage.input_tokens, None if usage is None else usage.output_tokens,
             actual, actual, request_id))
        self.db.commit()
        return {'request_id': request_id, 'usage_source': 'provider' if usage is not None else 'unknown',
                'currency': row['currency'], 'cost': None if actual is None else money_text(actual),
                'effective_max_tokens': row['effective_max_tokens']}

    def summary(self, novel_id: str = ''):
        rows = self.db.fetch_all('SELECT * FROM ai_usage_ledger WHERE novel_id=? ORDER BY created_at, id', (novel_id,))
        historical = self.db.fetch_all('''SELECT a.id, a.token_usage_json FROM ai_invocation_attempts a
            JOIN ai_invocation_sessions s ON s.id=a.session_id
            WHERE json_extract(s.context_json, '$.novel_id')=?
            AND NOT EXISTS (SELECT 1 FROM ai_usage_ledger l WHERE l.attempt_id=a.id)''', (novel_id,))
        for old in historical:
            usage = json.loads(old['token_usage_json'] or '{}')
            rows.append({'currency': 'UNKNOWN', 'actual_nanos': None, 'exposure_nanos': None,
                'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens')})
        budget = self.db.fetch_one('SELECT * FROM ai_usage_budgets WHERE novel_id=?', (novel_id,))
        currencies = {}
        for row in rows:
            item = currencies.setdefault(row['currency'], {'currency': row['currency'], 'known_nanos': 0,
                'exposure_nanos': 0, 'unknown_cost_calls': 0, 'unknown_usage_calls': 0,
                'input_tokens': 0, 'output_tokens': 0, 'calls': 0})
            item['calls'] += 1
            if row['actual_nanos'] is None:
                item['unknown_cost_calls'] += 1
            else:
                item['known_nanos'] += row['actual_nanos']
            item['exposure_nanos'] += row['exposure_nanos'] or 0
            if row['input_tokens'] is None or row['output_tokens'] is None:
                item['unknown_usage_calls'] += 1
            else:
                item['input_tokens'] += row['input_tokens']
                item['output_tokens'] += row['output_tokens']
        for item in currencies.values():
            item['known_cost'] = money_text(item.pop('known_nanos'))
            item['reserved_or_actual_cost'] = money_text(item.pop('exposure_nanos'))
            item['total_cost'] = None if item['unknown_cost_calls'] else item['known_cost']
            item['total_tokens'] = None if item['unknown_usage_calls'] else item['input_tokens'] + item['output_tokens']
        return {'novel_id': novel_id, 'calls': len(rows), 'historical_calls': len(historical), 'currencies': list(currencies.values()),
                'budget': None if budget is None else {'currency': budget['currency'],
                    'limit': None if budget['limit_nanos'] is None else money_text(budget['limit_nanos'])}}

    def entries(self, novel_id: str):
        rows = self.db.fetch_all('SELECT * FROM ai_usage_ledger WHERE novel_id=? ORDER BY created_at DESC, id LIMIT 200', (novel_id,))
        for row in rows:
            row['cost'] = None if row['actual_nanos'] is None else money_text(row.pop('actual_nanos'))
            row['reserved_cost'] = None if row['exposure_nanos'] is None else money_text(row.pop('exposure_nanos'))
        return rows

    def reconcile(self, request_id: str, *, cost: str, reason: str):
        if not reason.strip():
            raise ValueError('请填写上游账单依据')
        nanos = money_nanos(cost)
        reconciliation_id = str(uuid.uuid4())
        with self.db.transaction() as connection:
            connection.execute('''INSERT INTO ai_usage_reconciliations (id, request_id, currency, cost_nanos, reason)
                SELECT ?, id, currency, ?, ? FROM ai_usage_ledger
                WHERE id=? AND actual_nanos IS NULL AND status<>'running'
                ON CONFLICT(request_id) DO NOTHING''', (reconciliation_id, nanos, reason.strip(), request_id))
            cursor = connection.execute('''UPDATE ai_usage_ledger SET actual_nanos=?, exposure_nanos=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND actual_nanos IS NULL AND status<>'running'
                AND EXISTS (SELECT 1 FROM ai_usage_reconciliations WHERE id=? AND request_id=?)''',
                (nanos, nanos, request_id, reconciliation_id, request_id))
        if cursor.rowcount != 1:
            raise ValueError('只有已结束且金额未知的调用可以核对')
        row = self.db.fetch_one('SELECT novel_id FROM ai_usage_ledger WHERE id=?', (request_id,))
        return self.summary(row['novel_id'])
