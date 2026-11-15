"""Prep Calculator: water, food, battery, solar, generator fuel and bleach sums, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP), eight tools

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

Small language models are poor at arithmetic, so the sums are done here in plain
code, from the planning figures in the table below. Every answer repeats the
inputs, gives US and metric units and names the assumptions used. It runs on this
computer only, uses no internet and stores nothing.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import time
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = "prep-calculator"
VERSION = "1.0.0"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
BODY_WAIT_S = 5.0                       # the whole wait for a request body
REFUSED_MOST, REFUSED_S = 64 * 1024, 2.0            # what a request without the token may send before its 401
DROP_MOST, DROP_S = 64 * 1024 * 1024, 20.0          # a body over the cap is read and dropped, then a plain 400
IDLE_S = 1.0                                        # dropping stops when nothing more arrives for this long

# =============================================================================================
# THE CONSTANTS. Every one is a planning figure from widely published public preparedness
# guidance, or an exact unit definition. Planning figures are rough; real needs vary, and local
# official guidance wins. Change a figure here and every tool uses the new one.
# =============================================================================================
FIGURES = {
    # --- unit definitions (exact, not planning figures)
    "liters_per_us_gallon": 3.785411784,
    "liters_per_us_quart": 0.946352946,
    "kg_per_pound": 0.45359237,
    "grams_per_ounce": 28.349523125,           # avoirdupois ounce (weight, not fluid ounce)
    "km_per_mile": 1.609344,
    # --- water (planning figure)
    "water_gal_per_person_day": 1.0,           # drinking and basic hygiene
    "water_gal_per_person_day_hot": 1.5,       # hot climate: store more
    "water_quarts_per_pet_day": 1.0,           # a medium dog; the caller may override
    # --- food (planning figure, kilocalories per person per day; the caller may override)
    "kcal_adult_day": 2000,
    "kcal_child_day": 1500,                    # a middle figure for children; small children need less
    "kcal_heavy_work_day": 3000,               # hard physical work
    # --- shelf foods (planning figure, approximate kilocalories per 100 g, as packed)
    "foods_kcal_per_100g": {
        "white rice (dry)": 365,
        "dried beans (dry)": 340,
        "rolled oats (dry)": 380,
        "peanut butter": 590,
        "canned beans (drained weight, varies)": 100,
    },
    # --- batteries (planning figure)
    "inverter_efficiency": 0.85,               # share of battery energy that reaches an AC load
    "usable_lifepo4": 0.80,                    # usable depth of discharge, lithium iron phosphate
    "usable_lead_acid": 0.50,                  # usable depth of discharge, lead acid (flooded, AGM, gel)
    # --- solar (planning figure)
    "peak_sun_hours": 4.0,                     # per day; the caller may override
    "solar_system_efficiency": 0.75,           # panel watts to stored energy; the caller may override
    "battery_volts": 12.0,                     # to show a battery size in amp-hours
    # --- generator fuel (planning figure, rule of thumb)
    "gasoline_gal_per_hour_low": 0.5,          # a 3 to 5 kW gasoline generator near half load
    "gasoline_gal_per_hour_high": 0.75,
    "half_load_watts": 2500,                   # loads up to this use the range as is
    "max_generator_load_watts": 5000,          # above this the rule of thumb does not apply
    "propane_gal_per_gasoline_gal": 1.31,      # same energy: about 120,000 BTU per gallon of gasoline
                                               # against about 91,500 BTU per gallon of propane
    "propane_lb_per_gal": 4.2,
    # --- bleach for water (public guidance, 5 to 9 percent plain unscented household bleach)
    "bleach_min_percent": 5.0,
    "bleach_max_percent": 9.0,
    "bleach_drops_per_gallon": 8,              # clear water; doubled for cloudy water
    "bleach_drops_per_liter": 2,
    "bleach_tsp_per_gallon": 0.125,            # widely published 1/8 teaspoon per gallon, approximate
    "ml_per_teaspoon": 5.0,                    # about; 1 US teaspoon is 4.93 ml
}
F = FIGURES

# Ranges every input is checked against.
MAX_PEOPLE = 1000
MAX_DAYS = 1000
MAX_PETS = 100

GEN_SAFETY = "Run a generator outdoors only, far from doors and windows. Carbon monoxide kills."
BLEACH_SAFETY = ("Stir and wait 30 minutes. The water should smell slightly of chlorine. Bleach does not remove "
                 "chemicals or fuel. Boiling is the surer method when you can do it.")
ROUGH = "Planning figures only; local official guidance wins."


# ---------------------------------------------------------------- checked input

_NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _number(value, name: str, lo: float, hi: float, whole: bool = False):
    """A finite number from *lo* to *hi* (a whole one when *whole*), or a plain ValueError. Words are a number
    only when written as digits, one decimal point, an optional leading minus and an optional exponent."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, str) and (len(value) > 40 or not _NUMBER.fullmatch(value.strip())):   # capped first
        raise ValueError(f"{name} must be a number written in digits, like 12 or 2.5")
    try:
        x = float(value.strip()) if isinstance(value, str) else float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{name} is a number from {lo:,g} to {hi:,g}")
    if whole:
        if x != int(x):
            raise ValueError(f"{name} is a whole number")
        return int(x)
    return x


def _yes(value, name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):            # small models send 1 and 0
        return value == 1
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "1", "false", "no", "0", ""):
        return value.strip().lower() in ("true", "yes", "1")
    raise ValueError(f"{name} is true or false (yes or no, 1 or 0)")


def _text(value, name: str, most: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = " ".join(value.split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value


def n(x: float, sig: int | None = None, up: bool = False) -> str:
    """A number for a person, rounded half up: 1,234 or 56 or 8.7 or 0.63. With *sig*, that many
    significant figures. With *up*, rounded up instead, so a dose is never shown smaller than it is."""
    exact = x if isinstance(x, Decimal) else Decimal(repr(float(x)))
    x = float(x)
    if x == 0:
        return "0"
    if sig:
        places = max(0, sig - (math.floor(math.log10(abs(x))) + 1))
    else:
        places = 0 if abs(x) >= 100 else 1 if abs(x) >= 1 else 2
    q = exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_CEILING if up else ROUND_HALF_UP)
    s = f"{q:,f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _plural(x: float, one: str, many: str, up: bool = False) -> str:
    shown = n(x, up=up)
    return f"{shown} {one if shown == '1' else many}"


def _duration(hours: float) -> str:
    """'about 8.7 hours', 'about 41 minutes' under an hour, 'less than a minute' under a minute."""
    if hours * 60 < 1:
        return "less than a minute"
    if hours < 1:
        return f"about {_plural(hours * 60, 'minute', 'minutes')}"
    return f"about {_plural(hours, 'hour', 'hours')}"


def L(gallons: float) -> float:
    return gallons * F["liters_per_us_gallon"]


# ---------------------------------------------------------------- word lists

_BATTERY = {"lifepo4": "lifepo4", "lfp": "lifepo4", "lithium iron phosphate": "lifepo4",
            "lead acid": "lead_acid", "lead-acid": "lead_acid", "agm": "lead_acid", "gel": "lead_acid",
            "flooded": "lead_acid", "sla": "lead_acid"}
_BATTERY_NAME = {"lifepo4": "lithium iron phosphate", "lead_acid": "lead acid"}

_FOOD = {"white rice": "white rice (dry)", "rice": "white rice (dry)", "white rice (dry)": "white rice (dry)",
         "dried beans": "dried beans (dry)", "dry beans": "dried beans (dry)", "beans": "dried beans (dry)",
         "dried beans (dry)": "dried beans (dry)", "rolled oats": "rolled oats (dry)", "oats": "rolled oats (dry)",
         "oatmeal": "rolled oats (dry)", "rolled oats (dry)": "rolled oats (dry)", "peanut butter": "peanut butter",
         "canned beans": "canned beans (drained weight, varies)",
         "canned beans (drained weight, varies)": "canned beans (drained weight, varies)"}

_FUEL = {"gasoline": "gasoline", "gas": "gasoline", "petrol": "gasoline", "propane": "propane", "lpg": "propane"}

# unit_convert: each unit's kind and its size in the kind's base (liter, kilogram, kilometer, watt-hour).
_UNITS = {
    "gal": ("volume", F["liters_per_us_gallon"], "US gallon", "US gallons"),
    "qt": ("volume", F["liters_per_us_quart"], "US quart", "US quarts"),
    "l": ("volume", 1.0, "liter", "liters"),
    "lb": ("weight", F["kg_per_pound"], "pound", "pounds"),
    "oz": ("weight", F["grams_per_ounce"] / 1000, "ounce", "ounces"),
    "kg": ("weight", 1.0, "kilogram", "kilograms"),
    "g": ("weight", 0.001, "gram", "grams"),
    "mi": ("distance", F["km_per_mile"], "mile", "miles"),
    "km": ("distance", 1.0, "kilometer", "kilometers"),
    "f": ("temperature", 0, "degree Fahrenheit", "degrees Fahrenheit"),
    "c": ("temperature", 0, "degree Celsius", "degrees Celsius"),
    "wh": ("energy", 1.0, "watt-hour", "watt-hours"),
    "kwh": ("energy", 1000.0, "kilowatt-hour", "kilowatt-hours"),
    "ah": ("energy", 0, "amp-hour", "amp-hours"),
}
_UNIT_WORDS = {
    "gallon": "gal", "us gallon": "gal", "gal": "gal", "quart": "qt", "us quart": "qt", "qt": "qt",
    "liter": "l", "litre": "l", "l": "l", "pound": "lb", "lb": "lb", "lbs": "lb", "ounce": "oz", "oz": "oz",
    "kilogram": "kg", "kilo": "kg", "kg": "kg", "gram": "g", "g": "g", "mile": "mi", "mi": "mi",
    "kilometer": "km", "kilometre": "km", "km": "km", "fahrenheit": "f", "f": "f", "°f": "f",
    "celsius": "c", "centigrade": "c", "c": "c", "°c": "c", "watt-hour": "wh", "watt hour": "wh", "wh": "wh",
    "kilowatt-hour": "kwh", "kilowatt hour": "kwh", "kwh": "kwh", "amp-hour": "ah", "amp hour": "ah",
    "ampere-hour": "ah", "ah": "ah",
}


def _pick(value, name: str, table: dict, what: str) -> str:
    w = _text(value, name, 60).lower().replace("degrees ", "").replace("degree ", "")
    for c in (w, w.removesuffix("s")):
        if c in table:
            return table[c]
    raise ValueError(f"{value!r} is not {what}")


# ---------------------------------------------------------------- the tools

class Prep:
    def _water_day(self, people, pets, hot, pet_quarts):
        rate = F["water_gal_per_person_day_hot"] if hot else F["water_gal_per_person_day"]
        return rate, people * rate + pets * pet_quarts / 4

    def _who(self, people, pets, pet_quarts):
        who = _plural(people, "person", "people")
        if pets:
            who += (f" and {_plural(pets, 'pet', 'pets')} at {n(pet_quarts)} US quart"
                    f"{'' if pet_quarts == 1 else 's'} (about {_plural(pet_quarts * F['liters_per_us_quart'], 'liter', 'liters')}) "
                    f"per pet per day")
        return who

    def water_needed(self, people, days, pets=0, hot_climate=False, pet_quarts_per_day=None):
        people = _number(people, "people", 1, MAX_PEOPLE, whole=True)
        days = _number(days, "days", 1, MAX_DAYS)
        pets = _number(pets or 0, "pets", 0, MAX_PETS, whole=True)
        hot = _yes(hot_climate, "hot_climate")
        pq = _number(F["water_quarts_per_pet_day"] if pet_quarts_per_day is None else pet_quarts_per_day,
                     "pet_quarts_per_day", 0.1, 20)
        rate, per_day = self._water_day(people, pets, hot, pq)
        gal = per_day * days
        line = (f"For {self._who(people, pets, pq)}, for {_plural(days, 'day', 'days')}, at the planning figure of "
                f"{n(rate)} US gallon{'' if rate == 1 else 's'} ({n(L(rate))} liters) per person per day for drinking "
                f"and basic hygiene{' (raised for a hot climate)' if hot else ''}, store "
                f"{_plural(gal, 'US gallon', 'US gallons')} ({_plural(L(gal), 'liter', 'liters')}). That is about {_plural(math.ceil(gal - 1e-9), 'one-gallon jug', 'one-gallon jugs')}, "
                f"{_plural(math.ceil(gal / 5 - 1e-9), 'five-gallon can', 'five-gallon cans')} or "
                f"{_plural(math.ceil(L(gal) / 20 - 1e-9), 'twenty-liter can', 'twenty-liter cans')} (rounded up). ")
        if not hot:
            line += "Store more for a hot climate, pregnancy, nursing or illness. "
        else:
            line += "Store more still for pregnancy, nursing or illness. "
        return line + ROUGH

    def water_on_hand(self, people, gallons=None, liters=None, pets=0, hot_climate=False, pet_quarts_per_day=None):
        if (gallons is None) == (liters is None):
            raise ValueError("give the water as gallons or as liters, one of the two")
        if gallons is not None:
            gal = _number(gallons, "gallons", 0.1, 100_000)
        else:
            gal = _number(liters, "liters", 0.1, 400_000) / F["liters_per_us_gallon"]
        people = _number(people, "people", 1, MAX_PEOPLE, whole=True)
        pets = _number(pets or 0, "pets", 0, MAX_PETS, whole=True)
        hot = _yes(hot_climate, "hot_climate")
        pq = _number(F["water_quarts_per_pet_day"] if pet_quarts_per_day is None else pet_quarts_per_day,
                     "pet_quarts_per_day", 0.1, 20)
        rate, per_day = self._water_day(people, pets, hot, pq)
        days = gal / per_day
        full = math.floor(days + 1e-9)
        lasts = f"about {_plural(days, 'day', 'days')}" if days >= 1 else _duration(days * 24)
        return (f"{_plural(gal, 'US gallon', 'US gallons')} ({_plural(L(gal), 'liter', 'liters')}) for "
                f"{self._who(people, pets, pq)} lasts {lasts} ({full:,} full day{'' if full == 1 else 's'}), using "
                f"{_plural(per_day, 'US gallon', 'US gallons')} ({_plural(L(per_day), 'liter', 'liters')}) a day at the planning figure of {n(rate)} US "
                f"gallon{'' if rate == 1 else 's'} ({n(L(rate))} liters) per person per day for drinking and basic "
                f"hygiene{' (raised for a hot climate)' if hot else ''}. It runs out sooner in heat, for pregnancy, "
                f"nursing or illness, or if you cook with it. {ROUGH}")

    def food_needed(self, days, adults=0, children=0, heavy_workers=0, kcal_per_adult=None, kcal_per_child=None,
                    kcal_per_heavy_worker=None, food=None, kcal_per_100g=None):
        days = _number(days, "days", 1, MAX_DAYS)
        groups = [
            ("adult", "adults", _number(adults or 0, "adults", 0, MAX_PEOPLE, whole=True),
             _number(F["kcal_adult_day"] if kcal_per_adult is None else kcal_per_adult, "kcal_per_adult", 500, 6000)),
            ("child", "children", _number(children or 0, "children", 0, MAX_PEOPLE, whole=True),
             _number(F["kcal_child_day"] if kcal_per_child is None else kcal_per_child, "kcal_per_child", 500, 6000)),
            ("heavy worker", "heavy workers",
             _number(heavy_workers or 0, "heavy_workers", 0, MAX_PEOPLE, whole=True),
             _number(F["kcal_heavy_work_day"] if kcal_per_heavy_worker is None else kcal_per_heavy_worker,
                     "kcal_per_heavy_worker", 500, 6000)),
        ]
        if not 1 <= sum(g[2] for g in groups) <= MAX_PEOPLE:
            raise ValueError(f"give at least 1 and at most {MAX_PEOPLE:,} people across adults, children and "
                             "heavy_workers")
        if kcal_per_100g is not None:
            label = _text(food, "food", 40) or "your food"
            per100 = _number(kcal_per_100g, "kcal_per_100g", 10, 900)
            source = "the figure you gave"
        else:
            label = _pick(food or "white rice", "food", _FOOD,
                          "a food in the list (white rice, dried beans, rolled oats, peanut butter, canned beans); "
                          "give kcal_per_100g for any other food")
            per100 = F["foods_kcal_per_100g"][label]
            source = "an approximate planning figure"
        per_day = sum(g[2] * g[3] for g in groups)
        total = per_day * days
        grams = total / per100 * 100
        kg = grams / 1000
        who = ", ".join(f"{_plural(g[2], g[0], g[1])} at {n(g[3])} kcal a day" for g in groups if g[2])
        return (f"For {who}, for {_plural(days, 'day', 'days')}: {n(per_day)} kcal a day, {n(total)} kcal in all. "
                f"From {label} alone at about {n(per100)} kcal per 100 g ({source}), that is roughly {n(kg)} kg "
                f"({_plural(kg / F['kg_per_pound'], 'pound', 'pounds')}). Defaults are planning figures: 2,000 kcal for an adult, "
                f"1,500 for a child, 3,000 for heavy work. These are rough planning figures; real needs vary with "
                f"age, size, work and health, and one food alone is not a balanced diet. {ROUGH}")

    def battery_runtime(self, load_watts, watt_hours=None, amp_hours=None, volts=None, battery_type="lifepo4",
                        inverter=True, inverter_efficiency_percent=None, usable_percent=None):
        load = _number(load_watts, "load_watts", 1, 100_000)
        if watt_hours is not None and amp_hours is None and volts is None:
            wh = _number(watt_hours, "watt_hours", 1, 1_000_000)
            cap = f"a {n(wh)} watt-hour"
        elif watt_hours is None and amp_hours is not None and volts is not None:
            ah = _number(amp_hours, "amp_hours", 1, 100_000)
            v = _number(volts, "volts", 1, 1000)
            wh = ah * v
            if wh > 1_000_000:
                raise ValueError("that battery is more than 1,000,000 watt-hours")
            cap = f"a {n(ah)} amp-hour, {n(v)} volt ({n(wh)} watt-hour)"
        else:
            raise ValueError("give the battery as watt_hours, or as amp_hours and volts together")
        kind = _pick(battery_type, "battery_type", _BATTERY, "a battery type (lifepo4 or lead acid)")
        usable = (_number(usable_percent, "usable_percent", 10, 100) / 100 if usable_percent is not None
                  else F["usable_" + kind])
        inv = _yes(True if inverter is None else inverter, "inverter")
        eff = ((_number(inverter_efficiency_percent, "inverter_efficiency_percent", 50, 100) / 100
                if inverter_efficiency_percent is not None else F["inverter_efficiency"]) if inv else 1.0)
        hours = wh * usable * eff / load
        steps = f"{n(wh)} x {n(usable, 2)} x {n(eff, 2)} / {n(load)}" if inv else f"{n(wh)} x {n(usable, 2)} / {n(load)}"
        days = f" (about {_plural(hours / 24, 'day', 'days')})" if hours >= 48 else ""
        return (f"{cap[0].upper()}{cap[1:]} {_BATTERY_NAME[kind]} battery running {n(load)} watts "
                f"{'through an inverter' if inv else 'directly on DC (no inverter)'} lasts "
                f"{_duration(hours)}{days} ({steps}). Assumes {n(usable * 100)} percent of the battery "
                f"is usable ({'the planning figure for ' + _BATTERY_NAME[kind] if usable_percent is None else 'as you gave'})"
                + (f" and {n(eff * 100)} percent inverter efficiency" if inv else " and no inverter loss")
                + ". Cold, age and high draw shorten it. " + ROUGH)

    def power_budget(self, devices, peak_sun_hours=None, system_efficiency_percent=None, battery_type="lifepo4",
                     days_of_storage=1, inverter=True, battery_volts=None):
        if not isinstance(devices, list) or not 1 <= len(devices) <= 50:
            raise ValueError("devices is a list of 1 to 50 devices, each {name, watts, hours_per_day}")
        rows, total = [], 0.0
        for i, d in enumerate(devices, 1):
            if not isinstance(d, dict):
                raise ValueError(f"device {i} must be an object with name, watts and hours_per_day")
            name = _text(d.get("name"), f"device {i} name", 40) or f"device {i}"
            w = _number(d.get("watts"), f"device {i} watts", 0, 10_000)
            h = _number(d.get("hours_per_day", d.get("hours")), f"device {i} hours_per_day", 0, 24)
            total += w * h
            rows.append(f"{name} {n(w)} W x {n(h)} h")
        if total <= 0:
            raise ValueError("those devices use no energy")
        sun = _number(F["peak_sun_hours"] if peak_sun_hours is None else peak_sun_hours, "peak_sun_hours", 0.5, 12)
        sys_eff = (F["solar_system_efficiency"] if system_efficiency_percent is None
                   else _number(system_efficiency_percent, "system_efficiency_percent", 10, 100) / 100)
        kind = _pick(battery_type, "battery_type", _BATTERY, "a battery type (lifepo4 or lead acid)")
        store_days = _number(days_of_storage, "days_of_storage", 0.5, 30)
        inv = _yes(True if inverter is None else inverter, "inverter")
        eff = F["inverter_efficiency"] if inv else 1.0
        volts = _number(F["battery_volts"] if battery_volts is None else battery_volts, "battery_volts", 1, 1000)
        usable = F["usable_" + kind]
        battery = total * store_days / (usable * eff)
        solar = total / (sun * sys_eff)
        return (f"{'; '.join(rows)}: {_plural(total, 'watt-hour', 'watt-hours')} ({n(total / 1000, 3)} kWh) a day. "
                f"Battery to cover {_plural(store_days, 'day', 'days')}: about "
                f"{_plural(battery, 'watt-hour', 'watt-hours')} ({_plural(battery / volts, 'amp-hour', 'amp-hours')} "
                f"at {_plural(volts, 'volt', 'volts')}) of {_BATTERY_NAME[kind]}, from "
                f"{n(total)} x {n(store_days)} / ({n(usable, 2)} usable"
                + (f" x {n(eff, 2)} inverter)" if inv else ", no inverter)")
                + f". Solar to refill it each day: about {_plural(solar, 'watt', 'watts')} of panels, from {n(total)} / ({n(sun)} "
                f"peak sun hours x {n(sys_eff, 2)} system efficiency). Peak sun hours change with place and "
                f"season; cloudy days need more battery or more panels. Not electrical advice. {ROUGH}")

    def generator_fuel(self, load_watts, hours, fuel="gasoline"):
        load = _number(load_watts, "load_watts", 1, F["max_generator_load_watts"])
        hours = _number(hours, "hours", 0.1, 720)
        kind = _pick(fuel, "fuel", _FUEL, "a fuel this tool has a rule of thumb for (gasoline or propane); "
                     "for diesel or any other fuel read the generator's own manual")
        scale = max(1.0, load / F["half_load_watts"])
        lo = F["gasoline_gal_per_hour_low"] * scale * hours
        hi = F["gasoline_gal_per_hour_high"] * scale * hours
        rule = (f"the rule of thumb that a 3 to 5 kW gasoline generator near half load burns about "
                f"{n(F['gasoline_gal_per_hour_low'])} to {n(F['gasoline_gal_per_hour_high'])} US gallons an hour"
                + (f", scaled up by {n(load)} / {n(F['half_load_watts'])} for a load above "
                   f"{n(F['half_load_watts'])} watts" if scale > 1 else " (a lighter load saves little fuel)"))
        if kind == "propane":
            r = F["propane_gal_per_gasoline_gal"]
            lo, hi = lo * r, hi * r
            amount = (f"roughly {n(lo)} to {n(hi)} US gallons of propane ({n(L(lo))} to {n(L(hi))} liters, about "
                      f"{n(lo * F['propane_lb_per_gal'])} to {n(hi * F['propane_lb_per_gal'])} pounds or "
                      f"{n(lo * F['propane_lb_per_gal'] * F['kg_per_pound'])} to "
                      f"{n(hi * F['propane_lb_per_gal'] * F['kg_per_pound'])} kg)")
            rule += (f", times about {n(r, 3)} for propane, which holds less energy per gallon, at about "
                     f"{n(F['propane_lb_per_gal'])} pounds per gallon")
        else:
            amount = f"roughly {n(lo)} to {n(hi)} US gallons of gasoline ({n(L(lo))} to {n(L(hi))} liters)"
        return (f"Running {n(load)} watts for {_plural(hours, 'hour', 'hours')} takes {amount}, from {rule}. "
                f"This is a rough range; read the generator's own manual for its real figures. {GEN_SAFETY}")

    def bleach_for_water(self, volume, bleach_strength_percent, unit="gallons", cloudy=False):
        u = _pick(unit, "unit", {"gallon": "gal", "gal": "gal", "us gallon": "gal", "liter": "l", "litre": "l",
                                 "l": "l"}, "gallons or liters")
        vol = _number(volume, "volume", 0.1, 1000 if u == "gal" else 4000)
        pct = _number(bleach_strength_percent, "bleach_strength_percent", 0.01, 100)
        if not F["bleach_min_percent"] <= pct <= F["bleach_max_percent"]:
            raise ValueError(f"{n(pct, 3)} percent bleach is outside the 5 to 9 percent that standard public guidance "
                             "covers. Follow the product label and official guidance")
        dirty = _yes(cloudy, "cloudy")
        times = 2 if dirty else 1
        gal = vol if u == "gal" else vol / F["liters_per_us_gallon"]
        per = F["bleach_drops_per_gallon"] if u == "gal" else F["bleach_drops_per_liter"]
        drops = math.ceil(vol * per * times - 1e-9)
        what = (f"{_plural(vol, 'US gallon', 'US gallons') if u == 'gal' else _plural(vol, 'liter', 'liters')} "
                f"({_plural(L(gal), 'liter', 'liters') if u == 'gal' else _plural(gal, 'US gallon', 'US gallons')})")
        line = (f"For {what} of {'cloudy' if dirty else 'clear'} water with {n(pct, 3)} percent plain unscented household "
                f"bleach: {drops:,} drop{'' if drops == 1 else 's'} ({per} drops per {'gallon' if u == 'gal' else 'liter'} "
                f"x {n(vol)}{' x 2 for cloudy water' if dirty else ''}, rounded up).")
        if gal >= 2:
            # the drops are the dose; the spoon and ml figures are rounded UP, never shown smaller than they are
            tsp = Decimal(repr(gal)) * Decimal(repr(F["bleach_tsp_per_gallon"])) * times
            ml = tsp * Decimal(repr(F["ml_per_teaspoon"]))
            line += (f" That is about {_plural(tsp, 'teaspoon', 'teaspoons', up=True)} (about {n(ml, up=True)} ml), "
                     f"rounded up, using the widely published 1/8 teaspoon per gallon{', doubled' if dirty else ''}, "
                     f"which is approximate.")
        if dirty:
            line += " Let cloudy water settle and filter it through a clean cloth first."
        return line + " Use bleach with no scent or added cleaners. " + BLEACH_SAFETY

    def unit_convert(self, value, from_unit, to_unit, volts=None):
        x = _number(value, "value", -1_000_000_000, 1_000_000_000)
        a = _pick(from_unit, "from_unit", _UNIT_WORDS, "a unit this tool knows")
        b = _pick(to_unit, "to_unit", _UNIT_WORDS, "a unit this tool knows")
        ka, kb = _UNITS[a][0], _UNITS[b][0]
        if ka != kb:
            raise ValueError(f"{_UNITS[a][3]} are {ka} and {_UNITS[b][3]} are {kb}; they do not convert")
        note = ""
        if ka == "temperature":
            if (x - 32) * 5 / 9 < -273.15 if a == "f" else x < -273.15:
                raise ValueError("that is colder than absolute zero")
            y = (x - 32) * 5 / 9 if a == "f" and b == "c" else x * 9 / 5 + 32 if a == "c" and b == "f" else x
        else:
            if x < 0:
                raise ValueError("value must not be negative")
            if ka == "energy" and "ah" in (a, b) and a != b:
                if volts is None:
                    raise ValueError("watt-hours and amp-hours convert only at a given voltage; give volts")
                v = _number(volts, "volts", 1, 1000)
                wh = x * v if a == "ah" else x * _UNITS[a][1]
                y = wh / v if b == "ah" else wh / _UNITS[b][1]
                note = f" at {n(v)} volts (watt-hours = amp-hours x volts)"
            elif a == b:
                y = x
            else:
                y = x * _UNITS[a][1] / _UNITS[b][1]
                if ka == "volume":
                    note = " (US measures)"
        name_a = _UNITS[a][2] if n(x, 4) == "1" else _UNITS[a][3]
        name_b = _UNITS[b][2] if n(y, 4) == "1" else _UNITS[b][3]
        return f"{n(x, 4)} {name_a} is {n(y, 4)} {name_b}{note}."


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str, **more) -> dict:
    return {"type": kind, "description": text, **more}


_PEOPLE = _p("integer", "How many people, 1 to 1,000.")
_PETS = _p("integer", "Optional: how many pets. Default 0.")
_PETQ = _p("number", "Optional: US quarts of water per pet per day. Default 1 (a medium dog).")
_HOT = _p("boolean", "Optional: true for a hot climate (1.5 US gallons per person per day). Default false.")
_BTYPE = _p("string", "lifepo4 (lithium iron phosphate, 80 percent usable) or lead acid (50 percent). "
                      "Default lifepo4.")
TOOLS = [
    ("water_needed", "Work out how much drinking and hygiene water to store for a number of people (and pets) "
     "for a number of days, in US gallons and liters, and how many jugs or cans that is.",
     {"people": _PEOPLE, "days": _p("number", "How many days, 1 to 1,000."), "pets": _PETS, "hot_climate": _HOT,
      "pet_quarts_per_day": _PETQ}, ["people", "days"]),
    ("water_on_hand", "Work out how many days the water you have will last for a number of people (and pets).",
     {"gallons": _p("number", "The water you have in US gallons (or give liters)."),
      "liters": _p("number", "The water you have in liters (or give gallons)."),
      "people": _PEOPLE, "pets": _PETS, "hot_climate": _HOT, "pet_quarts_per_day": _PETQ}, ["people"]),
    ("food_needed", "Work out the food energy in kilocalories that adults, children and heavy workers need for a "
     "number of days, and roughly how much of one shelf food (rice, dried beans, oats, peanut butter, canned "
     "beans) gives that.",
     {"days": _p("number", "How many days, 1 to 1,000."),
      "adults": _p("integer", "Adults. Default 0."), "children": _p("integer", "Children. Default 0."),
      "heavy_workers": _p("integer", "People doing hard physical work. Default 0."),
      "kcal_per_adult": _p("number", "Optional: kcal per adult per day. Default 2,000."),
      "kcal_per_child": _p("number", "Optional: kcal per child per day. Default 1,500."),
      "kcal_per_heavy_worker": _p("number", "Optional: kcal per heavy worker per day. Default 3,000."),
      "food": _p("string", "Optional: white rice, dried beans, rolled oats, peanut butter or canned beans. "
                           "Default white rice. Any other name needs kcal_per_100g."),
      "kcal_per_100g": _p("number", "Optional: kcal per 100 g of the food, from its label.")}, ["days"]),
    ("battery_runtime", "Work out how many hours a battery runs a load, from its watt-hours (or amp-hours and "
     "volts), its type and whether the load runs through an inverter.",
     {"load_watts": _p("number", "The load in watts."),
      "watt_hours": _p("number", "Battery capacity in watt-hours (or give amp_hours and volts)."),
      "amp_hours": _p("number", "Battery capacity in amp-hours, with volts."),
      "volts": _p("number", "Battery voltage, with amp_hours."),
      "battery_type": _BTYPE,
      "inverter": _p("boolean", "true when the load runs through an inverter (85 percent efficient). Default true."),
      "inverter_efficiency_percent": _p("number", "Optional: inverter efficiency in percent. Default 85."),
      "usable_percent": _p("number", "Optional: usable share of the battery in percent.")}, ["load_watts"]),
    ("power_budget", "Add up the watt-hours a day for a list of devices, and the battery size and solar panel "
     "watts that would cover it.",
     {"devices": _p("array", "The devices, each with name, watts and hours_per_day.",
                    items={"type": "object", "properties": {
                        "name": {"type": "string"}, "watts": {"type": "number"},
                        "hours_per_day": {"type": "number"}}, "required": ["watts", "hours_per_day"]}),
      "peak_sun_hours": _p("number", "Optional: peak sun hours a day. Default 4."),
      "system_efficiency_percent": _p("number", "Optional: solar system efficiency in percent. Default 75."),
      "battery_type": _BTYPE,
      "days_of_storage": _p("number", "Optional: days the battery should cover. Default 1."),
      "inverter": _p("boolean", "true when the devices run through an inverter. Default true."),
      "battery_volts": _p("number", "Optional: battery voltage for the amp-hour figure. Default 12.")},
     ["devices"]),
    ("generator_fuel", "Give a rough range of the fuel a small gasoline or propane generator burns for a load "
     "and a number of hours, with the carbon monoxide safety line.",
     {"load_watts": _p("number", "The load in watts, up to 5,000."),
      "hours": _p("number", "How many hours it runs."),
      "fuel": _p("string", "gasoline or propane. Default gasoline.")}, ["load_watts", "hours"]),
    ("bleach_for_water", "Work out how many drops of plain unscented household bleach (5 to 9 percent) make clear "
     "or cloudy water safer to drink, per public guidance, with the steps that follow.",
     {"volume": _p("number", "How much water."),
      "unit": _p("string", "gallons or liters. Default gallons."),
      "bleach_strength_percent": _p("number", "The bleach strength on its label, in percent, like 6 or 8.25."),
      "cloudy": _p("boolean", "true when the water is cloudy (twice the bleach). Default false.")},
     ["volume", "bleach_strength_percent"]),
    ("unit_convert", "Convert gallons, liters and quarts; pounds, kilograms, ounces and grams; Fahrenheit and "
     "Celsius; miles and kilometers; and watt-hours and amp-hours at a given voltage.",
     {"value": _p("number", "The number to convert."),
      "from_unit": _p("string", "The unit it is in, like gallons, lb, F, miles, Wh, Ah."),
      "to_unit": _p("string", "The unit wanted, like liters, kg, C, km, Ah, Wh."),
      "volts": _p("number", "Only for watt-hours and amp-hours: the battery voltage.")},
     ["value", "from_unit", "to_unit"]),
]
CHANGES: list[str] = []


def tool_list() -> list[dict]:
    return [{"name": t, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": t not in CHANGES}}
            for t, d, props, req in TOOLS]


def mcp_answer(app: Prep, msg) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if not isinstance(mid, (str, int, type(None))) or isinstance(mid, bool):
        mid = None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def bad(text: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": text}}

    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return ok({"protocolVersion": asked if isinstance(asked, str) and asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        if not isinstance(params, dict):
            return bad("params must be an object.")
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or name not in {t[0] for t in TOOLS}:
            return bad("No tool by that name.")
        if not isinstance(args, dict):
            return ok({"content": [{"type": "text", "text": "arguments must be an object."}], "isError": True})
        try:
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (TypeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "one of those arguments is not one it takes"
            return ok({"content": [{"type": "text", "text": f"{name} could not do that: {reason}."}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Prep Calculator does not know that method."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "PrepCalculator/1"
    app: Prep
    token: str
    timeout = 15        # seconds; a stalled sender is cut off (set on the socket)

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _declared(self) -> int | None:
        """Content-Length, strictly: missing is 0; digits only; None when it is not a number."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
            return None
        return int(raw)

    def _drop(self, n_: int, most: int, seconds: float) -> None:
        """Read and throw away up to *n_* bytes of body (never more than *most*) within *seconds*, stopping early
        when nothing more arrives for IDLE_S. Nothing is left unread that the sender is still waiting to send,
        so the answer that follows is never lost to a reset."""
        left_bytes, end = min(n_, most), time.monotonic() + seconds
        try:
            while left_bytes > 0:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(min(left, IDLE_S))
                chunk = self.rfile.read1(min(left_bytes, 65536))
                if not chunk:
                    break
                left_bytes -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _length(self) -> int:
        """Content-Length, at most MAX_BODY bytes. A bigger body is read and dropped first (up to DROP_MOST
        within DROP_S seconds), so the plain 400 that follows reaches the sender."""
        k = self._declared()
        if k is None or k > MAX_BODY:
            self.close_connection = True
            self._drop(DROP_MOST if k is None else k, DROP_MOST, DROP_S)
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        return k

    def _body(self):
        n_ = self._length()
        raw = bytearray()
        end = time.monotonic() + BODY_WAIT_S
        try:
            while len(raw) < n_:                     # the whole body within BODY_WAIT_S, however it trickles in
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(n_ - len(raw))
                if not chunk:
                    break
                raw += chunk
        except OSError:
            raise ValueError(f"The request body did not all arrive within {BODY_WAIT_S:g} seconds.") from None
        finally:
            self.connection.settimeout(self.timeout)
        if len(raw) != n_:
            raise ValueError("The request ended before its body did.")
        try:
            return json.loads(bytes(raw).decode("utf-8"), parse_constant=_no_constant) if raw else {}
        except RecursionError:
            raise ValueError("The JSON is nested too deeply.") from None

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        k = self._declared()
        if k is None or k:                     # at most 64 KB, for at most 2 seconds, then the answer
            self._drop(REFUSED_MOST if k is None else k, REFUSED_MOST, REFUSED_S)
        self.close_connection = True
        self._json(401, {"error": "Prep Calculator needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Prep Calculator stopped on this request with an error "
                                          f"({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._send(405, b"", "text/plain")
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        self._json(404, {"error": "Prep Calculator has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Prep Calculator has no page here."})
        try:
            msg = self._body()
        except (ValueError, UnicodeDecodeError) as exc:
            self.close_connection = True
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": f"Not JSON. {exc}".strip()}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)

    def do_PUT(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        self.do_PUT()


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this calculator takes.")


def make_server(port: int, data=None, token: str | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. *data* is accepted for the common shape; nothing is stored.
    Call serve_forever(); stop with shutdown()."""
    handler = type("PrepHandler", (Handler,), {"app": Prep(), "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Prep Calculator for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    if not args.port:
        p.error("PuffPAI starts Prep Calculator with PUFFPAI_PORT (or --port) set.")
    server = make_server(args.port, os.environ.get("PUFFPAI_DATA"), os.environ.get("PUFFPAI_TOKEN"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
