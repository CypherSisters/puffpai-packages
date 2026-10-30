# Harvested from PuffPAIBuildBot@63c8a037:src/puffpai/app_data.py (adapted)
"""FreshTable: pantry, shopping list, meal plan and recipes, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``            the page (web/index.html), shown in the Desktop at /pkg/freshtable/
- ``/health``      "ok"
- ``/widget/list`` the shopping-list widget, ``{"items": [{"text"}]}``
- ``/data``        the whole document (GET, PUT), ``/updated`` its stamp
- ``/api/...``     brain help for the page (through the Core ``brain`` route)
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/freshtable/, which adds it.

The data is one JSON document in PUFFPAI_DATA (``freshtable.json``) with a
``last_updated`` stamp. Writes replace the whole file at once (temp file, then
rename). A file that exists but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "freshtable"
VERSION = "2.0.0"
DOC = "freshtable.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 25 * 1024 * 1024          # a phone photo fits; a stray upload does not


def _uid() -> str:
    return secrets.token_hex(6)


def _now() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"pantry": [], "shoppingList": [], "mealPlan": [], "recipes": [], "last_updated": ""}


class Store:
    """One JSON document with a stamp (the v1 pattern, app_data.py:348-370)."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return default_doc()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). FreshTable will not write over it.") from None
            if not isinstance(data, dict):
                raise Unreadable(f"{self.path} is not one JSON object. FreshTable will not write over it.")
            out = default_doc()
            out.update(data)
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".freshtable-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]

    def put_page(self, body: dict) -> tuple[int, dict]:
        """A page push. Keys the page did not send are kept (fix: the v1 page
        push deleted recipes, freshtable-app.html:437-447). If the page saw an
        older stamp than the saved one, nothing is written: the answer is 409
        with the saved document, so the page can reload it."""
        with self.lock:
            saved = self.load()
            base = body.get("last_updated")
            if base is not None and saved["last_updated"] and base != saved["last_updated"]:
                return 409, {"error": "FreshTable changed somewhere else. Here is the saved copy.", "data": saved}
            saved.update({k: v for k, v in body.items() if k != "last_updated"})
            return 200, {"last_updated": self.save(saved)}


# ------------------------------------------------------------- the logic
# From app_data.py:1023-1142 (fresh_*), plus the tools v1 only had in its page.

FOOD_CATEGORIES = {
    "Protein": ["chicken", "beef", "pork", "fish", "salmon", "shrimp", "turkey", "steak",
                "bacon", "sausage", "lamb", "tofu", "eggs", "egg"],
    "Produce": ["apple", "banana", "orange", "tomato", "lettuce", "spinach", "broccoli",
                "carrot", "onion", "pepper", "avocado", "potato", "garlic", "cucumber",
                "celery", "mushroom", "corn", "berry", "grape", "lemon", "lime", "mango"],
    "Dairy": ["milk", "cheese", "yogurt", "butter", "cream", "sour cream", "cottage"],
    "Grain": ["bread", "rice", "pasta", "flour", "oat", "cereal", "tortilla", "noodle"],
    "Pantry": ["oil", "vinegar", "sauce", "soup", "can", "bean", "sugar", "salt",
               "spice", "honey", "peanut butter", "jam", "jelly", "ketchup", "mustard"],
    "Frozen": ["frozen", "ice cream", "pizza"],
    "Beverage": ["water", "juice", "soda", "coffee", "tea", "beer", "wine"],
    "Snack": ["chips", "crackers", "cookies", "nuts", "popcorn", "candy", "chocolate"],
}


def guess_category(name: str) -> str:
    lower = name.lower()
    for cat, words in FOOD_CATEGORIES.items():
        if any(w in lower for w in words):
            return cat
    return "Other"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class FreshTable:
    def __init__(self, store: Store, brain: "Brain") -> None:
        self.store = store
        self.brain = brain

    # pantry
    def add_pantry(self, name: str, quantity: int = 1, unit: str = "pcs", category: str | None = None) -> str:
        name = name.strip()
        quantity = int(quantity)
        category = category or guess_category(name)
        with self.store.lock:
            data = self.store.load()
            for item in data["pantry"]:
                if item.get("name", "").lower() == name.lower():
                    item["quantity"] = int(item.get("quantity", 0)) + quantity
                    self.store.save(data)
                    return f"Updated {name}: now {item['quantity']} {item.get('unit', unit)} in the pantry."
            data["pantry"].append({
                "id": f"id_{_uid()}", "name": name, "emoji": "", "category": category,
                "quantity": quantity, "maxQuantity": max(quantity * 3, 5), "unit": unit,
                "priceEach": 0, "addedDate": _now(),
            })
            self.store.save(data)
        return f"Added {quantity} {unit} of {name} to the pantry ({category})."

    def list_pantry(self, filter: str | None = None) -> str:
        pantry = self.store.load()["pantry"]
        if not pantry:
            return "The pantry is empty."
        if filter and filter.lower() == "low":
            pantry = [i for i in pantry if i.get("quantity", 0) <= i.get("maxQuantity", 5) * 0.3]
            if not pantry:
                return "Nothing is running low in the pantry."
            label = "Running low"
        elif filter:
            pantry = [i for i in pantry if filter.lower() in str(i.get("category", "")).lower()]
            label = f"Pantry ({filter})"
        else:
            label = "Pantry"
        lines = [f"{i['name']}: {i.get('quantity', 0)} {i.get('unit', '')} ({i.get('category', '')})" for i in pantry]
        return f"{label} ({_plural(len(pantry), 'item')}):\n" + "\n".join(lines)

    # shopping list
    def add_shopping(self, name: str, quantity: int = 1, category: str | None = None) -> str:
        name = name.strip()
        category = category or guess_category(name)
        with self.store.lock:
            data = self.store.load()
            data["shoppingList"].append({
                "id": f"id_{_uid()}", "name": name, "quantity": int(quantity), "unit": "pcs",
                "category": category, "estimatedPrice": 0, "checked": False,
            })
            self.store.save(data)
        return f"Added {name} to the shopping list, quantity {int(quantity)}."

    def list_shopping(self) -> str:
        items = [i for i in self.store.load()["shoppingList"] if not i.get("checked")]
        if not items:
            return "The shopping list is empty."
        lines = [f"{i['name']}: {i.get('quantity', 1)}" for i in items]
        return f"Shopping list ({_plural(len(items), 'item')}):\n" + "\n".join(lines)

    def check_off_shopping(self, name: str) -> str:
        with self.store.lock:
            data = self.store.load()
            for item in data["shoppingList"]:
                if not item.get("checked") and item.get("name", "").lower() == name.strip().lower():
                    item["checked"] = True
                    self.store.save(data)
                    return f"Checked off {item['name']}."
        return f"{name} is not on the shopping list."

    def remove_shopping(self, name: str) -> str:
        with self.store.lock:
            data = self.store.load()
            keep = [i for i in data["shoppingList"] if i.get("name", "").lower() != name.strip().lower()]
            gone = len(data["shoppingList"]) - len(keep)
            if not gone:
                return f"{name} is not on the shopping list."
            data["shoppingList"] = keep
            self.store.save(data)
        return f"Removed {name} from the shopping list."

    def widget_list(self) -> dict:
        items = [i for i in self.store.load()["shoppingList"] if not i.get("checked")]
        return {"items": [{"text": i["name"] if int(i.get("quantity", 1)) == 1 else f"{i['name']} ({i['quantity']})"}
                          for i in items]}

    # meal plan
    def list_meal_plan(self) -> str:
        meals = self.store.load()["mealPlan"]
        if not meals:
            return "No meals are planned."
        return f"Meal plan ({_plural(len(meals), 'meal')}):\n" + "\n".join(
            f"{m.get('day', '')}: {m.get('name', '')}".strip(": ") for m in meals)

    def add_meal(self, day: str, name: str, ingredients: list[str] | None = None) -> str:
        with self.store.lock:
            data = self.store.load()
            data["mealPlan"].append({
                "id": f"id_{_uid()}", "day": day, "name": name, "time": "", "servings": 4,
                "ingredients": list(ingredients or []), "instructions": [], "theme": "custom", "date": _now(),
            })
            self.store.save(data)
        return f"Planned {name} for {day}."

    # recipes
    def add_recipe(self, name: str, ingredients: list[str] | str, instructions: str = "") -> str:
        if isinstance(ingredients, list):
            ingredients = ", ".join(str(i).strip() for i in ingredients if str(i).strip())
        ingredients = str(ingredients or "").strip()
        if isinstance(instructions, list):
            instructions = "\n".join(str(s).strip() for s in instructions if str(s).strip())
        with self.store.lock:
            data = self.store.load()
            data["recipes"].append({
                "id": f"r{_uid()}", "name": name.strip(), "ingredients": ingredients,
                "instructions": str(instructions or "").strip(), "createdAt": _now(),
            })
            self.store.save(data)
        count = len([i for i in ingredients.split(",") if i.strip()])
        return f"Saved the recipe {name.strip()} with {_plural(count, 'ingredient')}."

    def list_recipes(self) -> str:
        recipes = self.store.load()["recipes"]
        if not recipes:
            return "No recipes are saved."
        return f"Recipes ({_plural(len(recipes), 'recipe')}):\n" + "\n".join(
            f"{r['name']}: {r.get('ingredients', '')}" for r in recipes)

    def import_recipes(self, file_name: str = "", mime: str = "", content_b64: str = "", text: str = "") -> str:
        """Read recipes from a file (text or a photo) and save them. Fixes v1's
        refused import (ImportRecipes.tsx:84 got 403): this is the package's own tool."""
        kind = _file_kind(file_name, mime)
        if not text and content_b64:
            raw = base64.b64decode(content_b64)
            if kind == "image":
                found = self.brain.read_recipes_image(content_b64)
                return self._save_found(found, file_name)
            text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            return "That file has no text FreshTable can read."
        try:
            found = self.brain.read_recipes_text(text)
        except BrainUnavailable:
            found = parse_recipe_text(text)
        return self._save_found(found, file_name)

    def _save_found(self, found: list[dict], file_name: str) -> str:
        names = []
        for r in found:
            if not str(r.get("name", "")).strip():
                continue
            self.add_recipe(str(r["name"]), r.get("ingredients") or "", r.get("instructions") or "")
            names.append(str(r["name"]).strip())
        where = f" from {file_name}" if file_name else ""
        if not names:
            return f"No recipes found{where}."
        return f"Saved {_plural(len(names), 'recipe')}{where}: {', '.join(names)}."


def _file_kind(file_name: str, mime: str) -> str:
    ext = Path(file_name).suffix.lower()
    if mime.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        return "image"
    return "text"


_HEADS = re.compile(r"^\s*(ingredients?|instructions?|method|directions|steps)\s*:?\s*$", re.I)


def parse_recipe_text(text: str) -> list[dict]:
    """One recipe from plain text, without the brain: the first line is the
    name, lines under an "Ingredients" heading are ingredients, lines under
    "Instructions" (or Method, Directions, Steps) are the steps."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    name, part = lines[0].lstrip("# ").strip(), None
    ingredients, steps = [], []
    for ln in lines[1:]:
        head = _HEADS.match(ln)
        if head:
            part = "ing" if head.group(1).lower().startswith("ingredient") else "steps"
            continue
        clean = re.sub(r"^([-*•]|\d+[.)])\s*", "", ln)
        (ingredients if part == "ing" else steps).append(clean)
    return [{"name": name, "ingredients": ingredients, "instructions": "\n".join(steps)}]


# ------------------------------------------------------------- the brain

class BrainUnavailable(RuntimeError):
    pass


RECIPE_PROMPT = ('Read the recipes in this {what}. Answer with JSON only, like '
                 '{{"recipes": [{{"name": "...", "ingredients": ["..."], "instructions": "..."}}]}}.')


class Brain:
    """The bot's local model, only through the Core API ``brain`` route (02 needs.brain)."""

    def __init__(self, api: str | None, token: str | None) -> None:
        self.api = (api or "").rstrip("/")
        self.token = token or ""
        # The Core is on this computer: never send this call through the Bridge proxy.
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def chat(self, prompt: str, system: str = "", images: list[str] | None = None) -> str:
        if not self.api or not self.token:
            raise BrainUnavailable("FreshTable is not running under PuffPAI, so the brain is not reachable.")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = json.dumps({"messages": messages, "images": images or []}).encode()
        req = urllib.request.Request(self.api + "/v1/brain/chat", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with self._open(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BrainUnavailable(f"The brain did not answer ({exc}).") from None
        text = data.get("text") if isinstance(data, dict) else None    # {"text", "usage"}
        if not isinstance(text, str):
            raise BrainUnavailable("The brain answered without text.")
        return text

    def _recipes(self, answer: str) -> list[dict]:
        match = re.search(r"\{.*\}", answer, re.S)
        try:
            found = json.loads(match.group(0))["recipes"] if match else None
        except (ValueError, KeyError, TypeError):
            found = None
        if not isinstance(found, list):
            raise BrainUnavailable("The brain's answer had no recipe list.")
        return [r for r in found if isinstance(r, dict)]

    def read_recipes_text(self, text: str) -> list[dict]:
        return self._recipes(self.chat(RECIPE_PROMPT.format(what="text") + "\n\n" + text[:20000]))

    def read_recipes_image(self, image_b64: str) -> list[dict]:
        return self._recipes(self.chat(RECIPE_PROMPT.format(what="photo"), images=[image_b64]))


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("add_pantry", "Add a food item to the pantry, or add to its count if it is already there.",
     {"name": _p("string", "The food, like rice or milk."), "quantity": _p("integer", "How many. Default 1."),
      "unit": _p("string", "Like pcs, bags, lbs. Default pcs."), "category": _p("string", "Optional, like Dairy.")},
     ["name"]),
    ("list_pantry", "List what is in the pantry. Filter by a category, or use \"low\" for items running low.",
     {"filter": _p("string", "Optional: a category, or low.")}, []),
    ("add_shopping", "Put an item on the shopping list.",
     {"name": _p("string", "The item, like milk."), "quantity": _p("integer", "How many. Default 1."),
      "category": _p("string", "Optional, like Produce.")}, ["name"]),
    ("list_shopping", "List what is still on the shopping list.", {}, []),
    ("check_off_shopping", "Tick an item off the shopping list (bought).",
     {"name": _p("string", "The item to tick off.")}, ["name"]),
    ("remove_shopping", "Take an item off the shopping list.",
     {"name": _p("string", "The item to remove.")}, ["name"]),
    ("list_meal_plan", "List the meals planned for the week.", {}, []),
    ("add_meal", "Plan a meal for a day of the week.",
     {"day": _p("string", "Like Monday."), "name": _p("string", "The meal, like chicken fajitas."),
      "ingredients": {"type": "array", "items": {"type": "string"}, "description": "Optional ingredients."}},
     ["day", "name"]),
    ("add_recipe", "Save a recipe: its name, ingredients and instructions.",
     {"name": _p("string", "The recipe name."),
      "ingredients": {"type": "array", "items": {"type": "string"}, "description": "The ingredients."},
      "instructions": _p("string", "How to make it.")}, ["name", "ingredients"]),
    ("list_recipes", "List the saved recipes with their ingredients.", {}, []),
    ("import_recipes", "Read recipes from a file the owner sent (text or a photo of a recipe) and save them. "
     "A text file comes as file_name, mime and text; any other file as file_name, mime and content_b64.",
     {"file_name": _p("string", "The file's name, like pancakes.txt."),
      "mime": _p("string", "The file type, like text/plain or image/jpeg."),
      "content_b64": _p("string", "The file's bytes, base64."),
      "text": _p("string", "Or the recipe text itself.")}, []),
]
CHANGES = ["add_pantry", "add_shopping", "check_off_shopping", "remove_shopping",
           "add_meal", "add_recipe", "import_recipes"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: FreshTable, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        names = {t[0] for t in TOOLS}
        if name not in names:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        try:
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except BrainUnavailable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"FreshTable does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "FreshTable/2"
    app: FreshTable
    token: str

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    # helpers
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
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("That is too big for FreshTable.")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    # routes
    def do_HEAD(self):
        self.do_GET()

    def _refused(self) -> bool:
        """Every route needs the token (D48d). Read the body first, so the refusal arrives whole."""
        if self._token_ok():
            return False
        n = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        if n:
            self.rfile.read(n)
        self._json(401, {"error": "FreshTable needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def do_GET(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/widget/list":
                return self._json(200, self.app.widget_list())
            if path == "/data":
                return self._json(200, self.app.store.load())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"]})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "FreshTable has no page here."})

    def do_PUT(self):
        if self._refused():
            return
        if self._path() != "/data":
            return self._json(404, {"error": "FreshTable has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send the whole document as one JSON object."})
            code, out = self.app.store.put_page(body)
            self._json(code, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        try:
            body = self._body()
            if path == "/api/generate":
                text = self.app.brain.chat(str(body.get("prompt", "")), str(body.get("system", "")))
                return self._json(200, {"response": text})
            if path == "/api/read-image":
                text = self.app.brain.chat(str(body.get("prompt") or "Read the text in this photo."),
                                           images=[str(body.get("image_b64", ""))])
                return self._json(200, {"text": text})
            if path == "/api/import":
                msg = self.app.import_recipes(**{k: str(body.get(k, "")) for k in ("file_name", "mime", "content_b64", "text")})
                return self._json(200, {"message": msg})
        except BrainUnavailable as exc:
            return self._json(503, {"error": str(exc)})
        except Unreadable as exc:
            return self._json(409, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "FreshTable has no page here."})

    def do_DELETE(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def _mcp(self):
        try:
            msg = self._body()
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None, api: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = FreshTable(Store(data), Brain(api, token))
    handler = type("FreshTableHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="FreshTable for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts FreshTable with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"), os.environ.get("PUFFPAI_API"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
