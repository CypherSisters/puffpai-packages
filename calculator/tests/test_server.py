"""Calculator's server (calculator/server.py): the three tools, the token on every route and a refused
expression. Nothing here touches the internet."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20

_spec = importlib.util.spec_from_file_location("calculator_server", PKG / "server.py")
calc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calc)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.fixture
def base(tmp_path):
    with running(calc.make_server(0, tmp_path, TOKEN)) as url:
        yield url
    assert list(tmp_path.iterdir()) == []          # stores nothing


def mcp(base, method, params=None, token=TOKEN):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + "/mcp", data=body, method="POST", headers=h)
    try:
        with _open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def tool(base, name, **args):
    code, out = mcp(base, "tools/call", {"name": name, "arguments": args})
    assert code == 200
    return out["result"]["isError"], out["result"]["content"][0]["text"]


def test_calculate_reads_spoken_math_and_percentages(base):
    assert tool(base, "calculate", expression="What's 847 times 62?") == (False, "847 times 62 is 52,514.")
    assert tool(base, "calculate", expression="15% of 80") == (False, "15% of 80 is 12.")
    assert tool(base, "calculate", expression="80 + 15%") == (False, "80 + 15% is 92.")
    assert tool(base, "calculate", expression="round(sqrt(2) * pi, 3) + max(1, 2)")[1].endswith("is 6.443.")


def test_tip_and_convert(base):
    assert tool(base, "tip", bill=80, percent=15, people=4) == (
        False, "A 15% tip on 80.00 is 12.00, so the total is 92.00. Split between 4 people, each pays 23.00.")
    assert tool(base, "convert", value=5, from_unit="miles", to_unit="km") == (False, "5 miles is 8.04672 kilometres.")
    assert tool(base, "convert", value=350, from_unit="F", to_unit="C")[1] == \
        "350 degrees Fahrenheit is 176.667 degrees Celsius."
    assert tool(base, "convert", value=1, from_unit="cup", to_unit="ml")[1] == "1 US cup is 236.588 millilitres."


def test_every_route_needs_the_token(base):
    assert mcp(base, "tools/list", token=None)[0] == 401
    assert mcp(base, "tools/list", token="wrong")[0] == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        _open(urllib.request.Request(base + "/health"), timeout=10)
    assert e.value.code == 401
    with _open(urllib.request.Request(base + "/health", headers={"X-PuffPAI-Key": TOKEN}), timeout=10) as r:
        assert r.status == 200


@pytest.mark.parametrize("expression", [
    "9**9**9", "__import__('os').system('x')", "(1).__class__", "open('x')", "x + 1", "'a' * 3",
    "[1, 2]", "lambda: 1", "1/0", "1" * 201,
])
def test_anything_not_a_number_expression_is_refused(base, expression):
    is_error, text = tool(base, "calculate", expression=expression)
    assert is_error and text.startswith("calculate could not do that:")


def test_bad_inputs_to_tip_and_convert(base):
    assert tool(base, "tip", bill="lots")[0]
    assert tool(base, "tip", bill=10, people=0)[0]
    assert tool(base, "convert", value=3, from_unit="kg", to_unit="km")[0]
    assert tool(base, "convert", value=-500, from_unit="C", to_unit="K")[0]
