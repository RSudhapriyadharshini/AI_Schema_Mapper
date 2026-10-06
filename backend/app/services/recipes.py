"""Transform recipes: small, deterministic value transformations that Claude *writes once* per field.

A recipe is a list of steps such as [{"op": "split_list", "delimiters": [",", "|"]}]. Claude proposes it while
learning a source (it decides WHAT to do); ordinary code executes it on every later record (no LLM call).
A recipe is only trusted after it reproduces Claude's own answer for the sample value (see sources.py).

The vocabulary is deliberately small and closed. Nothing here is evaluated as code, and there are no
user-supplied regular expressions, so a recipe can only do what these steps do.
"""
import json
import re
from typing import Any

MAX_STEPS = 6


class RecipeError(ValueError):
    pass


_INT_RE = re.compile(r"\d+")
_DEC_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _usable(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return str(v).strip() != ""


def _text(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v).strip()


def _ci_get(d: dict, key: str):
    key = str(key).lower()
    for k, v in d.items():
        if str(k).lower() == key:
            return v
    return None


def _dedupe(items: list[str]) -> list[str]:
    out, seen = [], set()
    for x in items:
        t = x.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _split_list(v, p):
    if isinstance(v, list):
        return _dedupe([_text(x) for x in v if not isinstance(x, (list, dict))])
    return _dedupe(re.split("|".join(re.escape(d) for d in p["delimiters"]), _text(v)))


def _list_items(v, p):
    if not isinstance(v, list):
        raise RecipeError("list_items expects a list")
    return _dedupe([_text(x) for x in v if not isinstance(x, (list, dict))])


def _find_in_list(v, p):
    if not isinstance(v, list):
        raise RecipeError("find_in_list expects a list of objects")
    want = str(p["match_value"]).lower()
    for entry in v:
        if isinstance(entry, dict):
            mk = _ci_get(entry, p["match_key"])
            if mk is not None and want in str(mk).lower():
                rv = _ci_get(entry, p["return_key"])
                if _usable(rv):
                    return _text(rv)
    raise RecipeError("no matching entry")


def _map_objects(v, p):
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list):
        raise RecipeError("map_objects expects a list of objects")
    out = []
    for entry in v:
        if not isinstance(entry, dict):
            continue
        obj = {}
        for target, candidates in p["key_map"].items():
            for c in candidates:
                val = _ci_get(entry, c)
                if _usable(val) and not isinstance(val, (list, dict)):
                    obj[target] = _text(val)
                    break
        if obj:
            out.append(obj)
    return out


def _dict_get(v, p):
    if not isinstance(v, dict):
        raise RecipeError("dict_get expects an object")
    rv = _ci_get(v, p["key"])
    if not _usable(rv):
        raise RecipeError("key not present")
    return rv


def _first_item(v, p):
    if not isinstance(v, list) or not v:
        raise RecipeError("first_item expects a non-empty list")
    return v[0]


def _first_number(v, p):
    m = _INT_RE.search(_text(v))
    if not m:
        raise RecipeError("no number found")
    return m.group(0)


def _number(v, p):
    m = _DEC_RE.search(_text(v))
    if not m:
        raise RecipeError("no number found")
    return m.group(0)


def _zero_pad(v, p):
    s = _text(v)
    return s.zfill(p["width"]) if s.isdigit() else s


def _join(v, p):
    if not isinstance(v, list):
        raise RecipeError("join expects a list")
    return p["separator"].join(_text(x) for x in v)


OPS = {
    "copy": (lambda v, p: v, {}),
    "split_list": (_split_list, {"delimiters": "strlist"}),
    "list_items": (_list_items, {}),
    "find_in_list": (_find_in_list, {"match_key": "str", "match_value": "str", "return_key": "str"}),
    "map_objects": (_map_objects, {"key_map": "keymap"}),
    "dict_get": (_dict_get, {"key": "str"}),
    "first_item": (_first_item, {}),
    "first_number": (_first_number, {}),
    "number": (_number, {}),
    "zero_pad": (_zero_pad, {"width": "int"}),
    "join": (_join, {"separator": "str"}),
    "upper": (lambda v, p: _text(v).upper(), {}),
    "lower": (lambda v, p: _text(v).lower(), {}),
}


def validate_recipe(raw: Any) -> list[dict]:
    """Return a cleaned recipe (list of steps) or raise RecipeError. Accepts a JSON string or a list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise RecipeError("recipe is not valid JSON")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_STEPS:
        raise RecipeError(f"recipe must be a list of 1-{MAX_STEPS} steps")
    out = []
    for step in raw:
        if not isinstance(step, dict) or step.get("op") not in OPS:
            raise RecipeError(f"unknown step {step!r}")
        clean = {"op": step["op"]}
        for name, kind in OPS[step["op"]][1].items():
            val = step.get(name)
            if kind == "str" and isinstance(val, str) and val:
                clean[name] = val[:80]
            elif kind == "int" and isinstance(val, int) and not isinstance(val, bool) and 0 < val <= 20:
                clean[name] = val
            elif kind == "strlist" and isinstance(val, list) and 0 < len(val) <= 8 and all(isinstance(d, str) and 0 < len(d) <= 5 for d in val):
                clean[name] = val
            elif kind == "keymap" and isinstance(val, dict) and 0 < len(val) <= 8 and all(
                    isinstance(c, list) and 0 < len(c) <= 16 and all(isinstance(x, str) and x for x in c) for c in val.values()):
                clean[name] = {str(k): [x[:60] for x in c] for k, c in val.items()}
            else:
                raise RecipeError(f"step {step['op']}: bad or missing parameter '{name}'")
        out.append(clean)
    return out


def parse_raw(raw: Any) -> Any:
    """Source values arrive as text; lists/objects are JSON text. Give recipes the real structure."""
    if isinstance(raw, str):
        s = raw.strip()
        if s[:1] in ("[", "{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return s
        return s
    return raw


def run_recipe(recipe: list[dict], raw: Any) -> Any:
    value = parse_raw(raw)
    for step in recipe:
        value = OPS[step["op"]][0](value, step)
    return value


def result_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value).strip()
