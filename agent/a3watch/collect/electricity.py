"""
Live London electricity price → the yearly-cost budget is computed from the
current price, not a hardcoded number. Source: Octopus Agile, region C (London),
averaged over the last ~24h (a meaningful "average London cost at the time").
Refreshed at most once a day; on any failure we fall back to the configured
default and say so. Set [core].electricity_fetch=false to disable the network call.
"""
from __future__ import annotations

import json
import urllib.request
from . import Collector, Metric, Ctx, register

_UA = {"User-Agent": "a3watch/0.1"}
_REFRESH_S = 24 * 3600


def _get(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _fetch_london_price(region: str = "C"):
    """Return (gbp_per_kwh, source_str) or (None, None). Best-effort."""
    try:
        prods = _get("https://api.octopus.energy/v1/products/?page_size=100")
        code = None
        for p in prods.get("results", []):
            c = p.get("code", "")
            if "AGILE" in c and p.get("brand") == "OCTOPUS_ENERGY" and p.get("direction") == "IMPORT":
                code = c
                break
        if not code:
            return (None, None)
        tariff = f"E-1R-{code}-{region}"
        rates = _get(f"https://api.octopus.energy/v1/products/{code}/electricity-tariffs/"
                     f"{tariff}/standard-unit-rates/?page_size=48")
        vals = [r["value_inc_vat"] for r in rates.get("results", []) if "value_inc_vat" in r]
        if not vals:
            return (None, None)
        avg_pence = sum(vals) / len(vals)
        return (round(avg_pence / 100.0, 4), f"Octopus Agile {code} region {region}, 24h avg")
    except Exception:
        return (None, None)


def current_price(prev: dict, ts: float, cfg) -> dict:
    """Return {price, source, updated, live}. Cached daily; falls back to config."""
    cached = (prev or {}).get("electricity") or {}
    if cached.get("updated") and ts - cached["updated"] < _REFRESH_S:
        return cached
    price, source = (None, None)
    if getattr(cfg, "electricity_fetch", True):
        price, source = _fetch_london_price(getattr(cfg, "electricity_region", "C"))
    if price is None:
        return {"price": cfg.electricity_gbp_per_kwh, "source": "configured fallback",
                "updated": ts, "live": False}
    return {"price": price, "source": source, "updated": ts, "live": True}


class Electricity(Collector):
    name = "electricity"
    group = "Power & cost"

    def collect(self, ctx: Ctx) -> list[Metric]:
        price = ctx.price_gbp_kwh
        out = [Metric("price", num=round(price, 4), unit="£/kWh", series=True)]
        info = (ctx.prev.get("electricity") or {})
        out.append(Metric("price_source", text=info.get("source", "?")))
        out.append(Metric("price_live", num=1 if info.get("live") else 0,
                          text="live" if info.get("live") else "fallback"))
        # turn the £/yr budget into a continuous-power ceiling at the current price
        budget = getattr(ctx, "budget_gbp_year", 5.0)
        if price > 0:
            watt_ceiling = budget / (price / 1000.0 * 8760.0)
            out.append(Metric("budget_gbp_year", num=budget, unit="£/yr"))
            out.append(Metric("budget_power_ceiling", num=round(watt_ceiling, 3), unit="W"))
        return out


register(Electricity())
