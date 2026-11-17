"""Сборка таблиц данных одной командой: python -m pulse.data.build [--offline].

Каждая таблица проходит проверку по контракту и сохраняется с паспортом.
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from pulse.data import store
from pulse.data.loaders import cbu, market


def build(offline: bool = False) -> dict[str, dict]:
    passports = {}
    fx_usd = cbu.fetch(offline=offline)
    fx = pd.concat([fx_usd] + [cbu.fetch(ccy=c, offline=offline) for c in ("EUR", "RUB")], ignore_index=True)
    passports["fx"] = store.save(fx, "fx")
    fx = fx_usd
    passports["drugs"] = store.save(market.drugs(), "drugs")
    passports["prices"] = store.save(market.prices(), "prices")
    passports["fx_stats"] = cbu.annual_stats(fx)
    return passports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="не ходить в сеть, только сохранённые ответы")
    args = parser.parse_args()
    print(json.dumps(build(offline=args.offline), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
