# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compute-unit estimates for --json results and --budget (docs/08, phase D).

Colab reports the account's balance and its current hourly consumption rate
(all active runtimes together), not the cost of one runtime. With the
runtime's accelerator known, the meter estimates that runtime's own rate:

* alone, the account rate is exactly its rate (and is remembered per
  accelerator in settings.json, `cost_rates`);
* next to others, the remembered rate of its accelerator, else a default
  seen on a live account, else the account rate minus the others' rates;
* otherwise the account rate (an over-estimate: a budget stops early).

The rate is read once, when the command starts.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Rates seen on a live account (2026-11), until the account's own are learned.
DEFAULT_RATES = {"NONE": 0.08, "T4": 1.12}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _learned(settings_store) -> Dict[str, float]:
    if settings_store is None:
        return {}
    try:
        rates = settings_store.load().cost_rates
        return {k: v for k, v in rates.items() if _number(v)} if isinstance(rates, dict) else {}
    except Exception:
        return {}


def learn_rates(settings_store, accelerators: List[str], account_rate) -> None:
    """With exactly one runtime, the account rate is its accelerator's rate."""
    if settings_store is None or len(accelerators) != 1 or not _number(account_rate) or account_rate <= 0:
        return
    try:
        settings = settings_store.load()
        rates = dict(settings.cost_rates or {})
        if rates.get(accelerators[0]) == account_rate:
            return
        rates[accelerators[0]] = account_rate
        settings.cost_rates = rates
        settings_store.save(settings)
    except Exception as e:  # learning is a nicety; never fail a command for it
        logger.debug(f"Could not remember the compute-unit rate: {e}")


def runtime_rate(accelerator: str, accelerators: Optional[List[str]], account_rate: float,
                 learned: Dict[str, float]) -> Tuple[float, str]:
    """This runtime's hourly rate and how it was found (see the module doc)."""
    if accelerators is None:
        return account_rate, "account"
    if len(accelerators) <= 1:
        return account_rate, "only_runtime"
    if accelerator in learned:
        return learned[accelerator], "learned"
    if accelerator in DEFAULT_RATES:
        return DEFAULT_RATES[accelerator], "default"
    known = {**DEFAULT_RATES, **learned}
    others = list(accelerators)
    if accelerator in others:
        others.remove(accelerator)
    if all(o in known for o in others):
        rest = account_rate - sum(known[o] for o in others)
        if rest > 0:
            return rest, "account_minus_others"
    return account_rate, "account"


def _accelerators(client) -> Optional[List[str]]:
    try:
        assignments = client.list_assignments()
    except Exception:
        return None
    if not isinstance(assignments, list):
        return None
    out = []
    for a in assignments:
        value = getattr(getattr(a, "accelerator", None), "value", None)
        if not isinstance(value, str):
            return None
        out.append(value)
    return out


class CostMeter:
    def __init__(self, client, accelerator: Optional[str] = None, settings_store=None):
        accelerator = accelerator if isinstance(accelerator, str) else None
        self.accelerator = accelerator
        self.info = None
        self.basis = "account"
        self._rate = None
        try:
            info = client.get_consumption_user_info()
            numbers = (info.consumption_rate_hourly, info.paid_compute_units_balance)
            if all(_number(n) for n in numbers):
                self.info = info
        except Exception as e:  # a missing estimate must never fail a run
            logger.debug(f"Compute-unit info unavailable: {e}")
        if self.info is None:
            return
        account = self.info.consumption_rate_hourly
        self._rate = account
        if accelerator:
            accelerators = _accelerators(client)
            if accelerators is not None:
                learn_rates(settings_store, accelerators, account)
            self._rate, self.basis = runtime_rate(accelerator, accelerators, account, _learned(settings_store))

    @property
    def rate(self) -> Optional[float]:
        return self._rate

    def seconds_for(self, budget_cu: float) -> Optional[float]:
        """How long ``budget_cu`` lasts at this runtime's rate; None: no limit."""
        if not self.rate or self.rate <= 0:
            return None
        return budget_cu / self.rate * 3600

    def block(self, duration_s: float) -> Optional[Dict[str, Any]]:
        """The ``cost`` block of a result, or None without consumption info."""
        if self.info is None:
            return None
        rate = self.rate
        balance = self.info.paid_compute_units_balance
        account = self.info.consumption_rate_hourly
        return {
            "accelerator": self.accelerator,
            "rate_cu_per_hour": round(rate, 4),
            "account_rate_cu_per_hour": account,
            "rate_basis": self.basis,
            "balance_cu": balance,
            "used_cu_estimate": round(rate * duration_s / 3600, 3),
            "hours_left_at_rate": round(balance / account, 1) if account > 0 else None,
            "assignments": self.info.assignments_count,
        }
