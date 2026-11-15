# Prep Calculator: how each answer is worked out

Every figure below is in the `FIGURES` table at the top of `server.py`. All are planning figures except the unit definitions. Numbers are shown rounded half up: whole numbers from 100 up, one decimal from 1 to 100, two decimals below 1 (`unit_convert` shows four significant figures).

Unit definitions (exact): 1 US gallon = 3.785411784 liters; 1 US quart = 0.946352946 liters (a quarter gallon); 1 pound = 0.45359237 kg; 1 ounce (weight) = 28.349523125 g; 1 mile = 1.609344 km.

## 1. water_needed

1. Per person per day: 1 US gallon, or 1.5 US gallons when `hot_climate` is true.
2. Per pet per day: 1 US quart (0.25 gallon) for a medium dog, or `pet_quarts_per_day`.
3. Gallons a day = people x per-person figure + pets x pet quarts / 4.
4. Gallons = gallons a day x days. Liters = gallons x 3.785411784.
5. Containers, each rounded up: 1 gallon jugs = gallons; 5 gallon cans = gallons / 5; 20 liter cans = liters / 20.

Checks: people 1 to 1,000 (whole); days 1 to 1,000; pets 0 to 100 (whole); pet quarts 0.1 to 20.

## 2. water_on_hand

1. Take gallons, or liters / 3.785411784 (exactly one of the two).
2. Gallons a day as in water_needed steps 1 to 3.
3. Days = gallons / gallons a day; full days = that, rounded down.

Checks: gallons 0.1 to 100,000; liters 0.1 to 400,000; people and pets as above.

## 3. food_needed

1. Per person per day: adult 2,000 kcal, child 1,500 kcal, heavy work 3,000 kcal; each may be overridden (500 to 6,000).
2. Kcal a day = sum of people in each group x that group's figure. Total = kcal a day x days.
3. Food energy per 100 g (approximate): white rice (dry) 365; dried beans (dry) 340; rolled oats (dry) 380; peanut butter 590; canned beans (drained weight, varies) 100. Or the caller's `kcal_per_100g` (10 to 900).
4. Grams = total / kcal per 100 g x 100. Kilograms = grams / 1,000. Pounds = kilograms / 0.45359237.

Checks: each group 0 to 1,000 (whole), at least 1 person in all; days 1 to 1,000.

## 4. battery_runtime

1. Watt-hours = `watt_hours`, or `amp_hours` x `volts`.
2. Usable share: 80 percent for lithium iron phosphate, 50 percent for lead acid (flooded, AGM, gel), or `usable_percent`.
3. Inverter efficiency: 85 percent when `inverter` is true (or `inverter_efficiency_percent`); none counted when false.
4. Hours = watt-hours x usable share x inverter efficiency / load watts. From 48 hours up, days = hours / 24 is shown too.

Checks: load 1 to 100,000 W; capacity 1 to 1,000,000 Wh; amp-hours 1 to 100,000; volts 1 to 1,000.

## 5. power_budget

1. Watt-hours a day = sum of watts x hours a day for each device (at most 50 devices, 0 to 10,000 W, 0 to 24 h).
2. Battery watt-hours = watt-hours a day x days of storage (default 1) / (usable share x inverter efficiency), with the shares from tool 4 (inverter default true).
3. Battery amp-hours = battery watt-hours / battery volts (default 12).
4. Solar panel watts = watt-hours a day / (peak sun hours x system efficiency), defaults 4 hours and 75 percent.

Checks: peak sun hours 0.5 to 12; system efficiency 10 to 100 percent; days of storage 0.5 to 30.

## 6. generator_fuel

1. Rule of thumb: a 3 to 5 kW gasoline generator near half load burns about 0.5 to 0.75 US gallons an hour.
2. Loads up to 2,500 W use that range as is (a lighter load saves little fuel). From 2,500 to 5,000 W the range is multiplied by load / 2,500. Over 5,000 W is refused.
3. Gallons = rate x hours, low and high. Liters = gallons x 3.785411784.
4. Propane: gallons x 1.31 (gasoline about 120,000 BTU per gallon, propane about 91,500), pounds = gallons x 4.2.
5. Diesel and other fuels are refused: read the generator's own manual.
6. Always ends: "Run a generator outdoors only, far from doors and windows. Carbon monoxide kills."

Checks: load 1 to 5,000 W; hours 0.1 to 720.

## 7. bleach_for_water

1. Only plain unscented household bleach of 5 to 9 percent. Any other strength is refused: follow the product label and official guidance.
2. Drops = 8 per US gallon, or 2 per liter, x volume; x 2 for cloudy water; rounded up.
3. From 2 gallons up (or the same in liters), also: teaspoons = gallons x 1/8 (x 2 if cloudy), approximate; ml = teaspoons x about 5. The drops are the main figure; the teaspoons and ml are marked "about" and each is rounded UP to the shown precision, so a dose is never shown smaller than it is.
4. Always ends: "Stir and wait 30 minutes. The water should smell slightly of chlorine. Bleach does not remove chemicals or fuel. Boiling is the surer method when you can do it."

Checks: volume 0.1 to 1,000 gallons or 0.1 to 4,000 liters.

## 8. unit_convert

1. Volume, weight and distance: value x size of the from unit / size of the to unit, using the definitions above.
2. Temperature: C = (F - 32) x 5 / 9; F = C x 9 / 5 + 32. Below absolute zero is refused.
3. Energy: watt-hours = amp-hours x volts (volts required); 1 kWh = 1,000 Wh.

Checks: value -1,000,000,000 to 1,000,000,000; only temperatures may be negative.
