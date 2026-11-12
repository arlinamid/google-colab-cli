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
(all active runtimes together), not the cost of one run. So the estimate is
rate x duration: exact with one runtime, an over-estimate with several, which
makes a budget stop early rather than late. The rate is read once, when the
command starts.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CostMeter:
    def __init__(self, client, accelerator: Optional[str] = None):
        self.accelerator = accelerator
        self.info = None
        try:
            info = client.get_consumption_user_info()
            numbers = (info.consumption_rate_hourly, info.paid_compute_units_balance)
            if all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in numbers):
                self.info = info
        except Exception as e:  # a missing estimate must never fail a run
            logger.debug(f"Compute-unit info unavailable: {e}")

    @property
    def rate(self) -> Optional[float]:
        return self.info.consumption_rate_hourly if self.info else None

    def seconds_for(self, budget_cu: float) -> Optional[float]:
        """How long ``budget_cu`` lasts at the current rate; None: no limit."""
        if not self.rate or self.rate <= 0:
            return None
        return budget_cu / self.rate * 3600

    def block(self, duration_s: float) -> Optional[Dict[str, Any]]:
        """The ``cost`` block of a result, or None without consumption info."""
        if self.info is None:
            return None
        rate = self.info.consumption_rate_hourly
        balance = self.info.paid_compute_units_balance
        return {
            "accelerator": self.accelerator,
            "rate_cu_per_hour": rate,
            "balance_cu": balance,
            "used_cu_estimate": round(rate * duration_s / 3600, 3),
            "hours_left_at_rate": round(balance / rate, 1) if rate > 0 else None,
            "assignments": self.info.assignments_count,
        }
