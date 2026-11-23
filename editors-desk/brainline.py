"""The brain line and small file helpers, copied from Manuscript's analysis.py, since a package never imports a sibling.

Every model call goes to the Core's ``POST /v1/brain/chat`` with the package token, so the words stay on this computer.
One queue of calls per process, one at a time; the writer's own asks go ahead of a check's next call.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CALL_TIMEOUT = 960               # a little over the Core's own 900 seconds, so the Core gets to say so first
BUSY_WAIT = 30
BUSY_TRIES = 10
REPAIR = "Return only the JSON."


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tokens(text: str) -> int:
    """A rough count, high rather than low: 1.4 a word, or a quarter of the characters for JSON with few spaces."""
    return int(max(len(text.split()) * 1.4, len(text) / 4)) + 1


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()         # the same sha Manuscript's cache is keyed by


def write_atomic(path: Path, text: str) -> None:
    """The whole file at once: a temp file beside it, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(40):            # on Windows a page reading the old file holds it for a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, obj: Any) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(20):                # on Windows a file being replaced can't be opened for a moment
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 19:
                return default
            time.sleep(0.05)
        except (OSError, ValueError):
            return default
    return default


class BrainDown(Exception):
    """The brain can't answer now, or refused: a plain line for the owner."""


class BrainBusy(BrainDown):
    """The Core answered 503: the brain is busy with something else."""


class BrainSlow(Exception):
    """One call took longer than the time allowed."""


class Brain:
    def __init__(self, api: str | None, token: str | None, wait_s: float = BUSY_WAIT, tries: int = BUSY_TRIES,
                 timeout: float = CALL_TIMEOUT) -> None:
        self.api = (api or "").rstrip("/")
        self.token = token or ""
        self.wait_s, self.tries, self.timeout = wait_s, tries, timeout
        self.as_written: Callable[[], bool] = lambda: True    # the setting; the manifest declares it
        self.last: dict = {}                                  # the last answer's usage, context and model

    def chat(self, messages: list[dict], patient: bool = True) -> tuple[str, str]:
        """(text, model). ``patient``: on a 503 wait and try again. A tool the chat is waiting on says so at once."""
        body = json.dumps({"messages": messages, "as_written": bool(self.as_written())}).encode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # the Core is on this computer
        tries = self.tries if patient else 0
        busy = False
        for attempt in range(tries + 1):
            req = urllib.request.Request(self.api + "/v1/brain/chat", data=body, method="POST",
                                         headers={"Authorization": f"Bearer {self.token}",
                                                  "Content-Type": "application/json"})
            try:
                with opener.open(req, timeout=self.timeout) as r:
                    got = json.loads(r.read().decode("utf-8"))
                self.last = {"usage": got.get("usage") or {}, "context": got.get("context"), "model": got.get("model")}
                return str(got.get("text") or ""), str(got.get("model") or "")
            except urllib.error.HTTPError as exc:
                if exc.code != 503:
                    raise BrainDown(f"Your bot's brain refused the ask (code {exc.code}).") from None
                try:
                    state = json.loads(exc.read().decode("utf-8")).get("state")
                except (OSError, ValueError, AttributeError):
                    state = ""
                if state == "timed_out":     # it ran out of time on this ask: asking the same again would too
                    raise BrainSlow() from None
                busy = True
            except (socket.timeout, TimeoutError):
                raise BrainSlow() from None
            except (urllib.error.URLError, ConnectionError, OSError, ValueError, AttributeError):
                busy = False             # the Core is restarting, or the answer was cut: like a 503
            if attempt < tries:
                time.sleep(self.wait_s)
        raise (BrainBusy if busy else BrainDown)(
            "Your bot's brain could not answer for five minutes, so this check stopped. Press it again when your bot "
            "is ready; it goes on from where it was." if patient else
            "Your bot's brain can't answer right now. Try again in a minute.")


class NotYet(Exception):
    """A job in the queue did not finish in the time the caller would wait."""


class Job:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.started = False
        self.cancelled = False

    def run(self) -> None:
        try:
            self.result = self.fn()
        except Exception as exc:  # noqa: BLE001 - handed to whoever waits on the job
            self.error = exc
        finally:
            self.done.set()

    def wait(self, timeout: float | None = None) -> Any:
        if not self.done.wait(timeout):
            raise NotYet()
        if self.error is not None:
            raise self.error
        return self.result


class BrainQueue:
    """The one line of brain calls: one at a time. The writer's asks go ahead of a check's next call; a call already
    running always finishes first."""

    FRONT, CHECK = 0, 1

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.jobs: list[tuple[int, int, Job]] = []
        self.seq = itertools.count()
        threading.Thread(target=self._work, daemon=True, name="brain queue").start()

    def submit(self, fn: Callable[[], Any], rank: int = FRONT) -> Job:
        job = Job(fn)
        with self.cv:
            heapq.heappush(self.jobs, (rank, next(self.seq), job))
            self.cv.notify()
        return job

    def cancel(self, job: Job) -> bool:
        with self.cv:
            if job.started:
                return False
            job.cancelled = True
            return True

    def _work(self) -> None:
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                _rank, _seq, job = heapq.heappop(self.jobs)
                if job.cancelled:
                    continue
                job.started = True
            job.run()


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in text.lower():                     # an opening tag the template already sent
        text = text[text.lower().rindex("</think>") + len("</think>"):]
    return text.strip()


def parse_json(text: str) -> Any:
    """The first JSON object or list in the answer, or None."""
    s = strip_think(text)
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(s)
    except ValueError:
        pass
    for a, b in (("{", "}"), ("[", "]")):
        i, j = s.find(a), s.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                continue
    return None


def plain(text: Any, n: int = 3000) -> str:
    """One clean line of the model's words: no em dashes, the spacing tidied."""
    s = str(text or "") if not isinstance(text, (dict, list)) else ""
    s = re.sub(r"\s*[\u2014\u2013]\s*", ", ", s)
    return " ".join(s.split())[:n]
