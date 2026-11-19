"""Work out the answer from the data, not from the wording.

Given the tables from an attachment, the ask, and any rule text (an asterisk note, a
runbook line, a formula in code), produce short computed facts the draft can quote:
which rows break a threshold, how many fall in each band, and one worked example per band.
"""

from __future__ import annotations

import re

from hailports.clip.attach import Table, _num

_MONEY = re.compile(r"(?i)\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|mm|million|thousand|b)?\b|\b(\d{1,3}(?:,\d{3}){1,3}|\d{5,10})\b(?!\s*%)")
_OFFSET = re.compile(r"(?i)\+\s*\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m)?")
_MULT = re.compile(r"(?i)(?:\*|×|x)\s*(\d(?:\.\d+)?)\b|(\d{2,3})\s*%")
_RANGE = re.compile(
    r"(?i)(?:\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m)?\s*(?:<=|≤|to|-|–|and)\s*(?:\w+\s*(?:<=|≤)\s*)?\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m)?)"
)
_AMT = r"(\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:k|m|mm|million)?)"
_BETWEEN = re.compile(
    r"(?i)" + _AMT + r"\s*(?:<=|≤|<)\s*[\w_.]+\s*(?:<=|≤|<)\s*" + _AMT
    + r"|(?:from|between)\s+" + _AMT + r"\s+(?:to|and|-|–)\s+" + _AMT
    + r"|(\$\s?\d[\d,]*(?:\.\d+)?\s*(?:k|m|mm|million)?)\s*(?:-|–|to)\s*(\$\s?\d[\d,]*(?:\.\d+)?\s*(?:k|m|mm|million)?)"
)
_LABEL_HINT = ("child name", "parent name", "dealer", "account name", "customer", "name", "company", "vendor", "store", "region")
_VALUE_HINT = ("goal", "target", "quota", "budget", "amount", "revenue", "total", "price", "cost", "sales", "value")


def money(text: str) -> list[float]:
    out: list[float] = []
    for m in _MONEY.finditer(text or ""):
        raw = m.group(1) or m.group(3)
        if not raw:
            continue
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit in {"k", "thousand"}:
            val *= 1e3
        elif unit in {"m", "mm", "million"}:
            val *= 1e6
        elif unit == "b":
            val *= 1e9
        if 1900 <= val <= 2100 and not m.group(1):
            continue  # a year, not money
        if val >= 1000 and val not in out:
            out.append(val)
    return out


def _dollar(text: str) -> str:
    text = (text or "").strip()
    return text if text.startswith("$") else "$" + text


def fmt_money(val: float) -> str:
    sign = "-" if val < 0 else ""
    val = abs(val)
    if val >= 1e6:
        txt = f"{val / 1e6:.2f}".rstrip("0").rstrip(".")
        return f"{sign}${txt}m"
    if val >= 1e3:
        txt = f"{val / 1e3:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${txt}k"
    return f"{sign}${val:,.0f}"


def _cols(table: Table):
    nums, texts = [], []
    for idx, name in enumerate(table.header):
        vals = [row[idx] for row in table.rows if idx < len(row) and row[idx] not in (None, "")]
        if not vals:
            continue
        numeric = [v for v in vals if _num(v) is not None and not isinstance(v, str)]
        if len(numeric) >= 0.7 * len(vals):
            nums.append(idx)
        else:
            texts.append(idx)
    return nums, texts


def _score_header(name: str, words: set[str], hints: tuple[str, ...]) -> float:
    low = name.lower()
    score = 0.0
    for i, hint in enumerate(hints):
        if hint in low:
            score += 10 - i * 0.5
    score += sum(3 for w in words if len(w) > 2 and w in low)
    return score


def pick_columns(table: Table, ask: str) -> tuple[int | None, int | None]:
    words = set(re.findall(r"[a-z]{3,}", (ask or "").lower()))
    nums, texts = _cols(table)
    if not nums:
        return None, None
    idish = re.compile(r"(?i)(?:#|\bid\b|\bno\.?$|number|zip|code|year|phone)")
    pool = [i for i in nums if not idish.search(table.header[i])] or nums
    if not [i for i in nums if not idish.search(table.header[i])]:
        return None, None
    value = max(pool, key=lambda i: (_score_header(table.header[i], words, _VALUE_HINT), i))
    label = None
    if texts:
        label = max(texts, key=lambda i: (_score_header(table.header[i], words, _LABEL_HINT),
                                          len({str(r[i]) for r in table.rows if i < len(r)})))
    return label, value


def parse_rule(text: str) -> dict:
    """Pull an "offset inside a band, multiplier otherwise" rule from notes or an asterisk."""
    rule: dict = {}
    if not text:
        return rule
    for line in re.split(r"[\n;]|(?<=\.)\s", text):
        off = _OFFSET.search(line)
        if off and "offset" not in rule:
            val = float(off.group(1).replace(",", "")) * (1e3 if (off.group(2) or "").lower() == "k" else 1e6 if (off.group(2) or "").lower() == "m" else 1)
            if val >= 1000:
                rule["offset"] = val
        mul = _MULT.search(line)
        if mul and "mult" not in rule:
            if mul.group(1):
                val = float(mul.group(1))
                if 1.0 < val < 5:
                    rule["mult"] = val
            elif mul.group(2) and 100 < int(mul.group(2)) <= 300:
                rule["mult"] = int(mul.group(2)) / 100.0
    rng = _BETWEEN.search(text)
    if rng:
        a = rng.group(1) or rng.group(3) or rng.group(5)
        b = rng.group(2) or rng.group(4) or rng.group(6)
        lo, hi = money(_dollar(a)), money(_dollar(b))
        if lo and hi and lo[0] < hi[0]:
            rule["low"], rule["high"] = lo[0], hi[0]
            return rule
    amounts = sorted(set(money(text)))
    if "offset" in rule:
        amounts = [a for a in amounts if a != rule["offset"]] or amounts
    if len(amounts) >= 2:
        rule["low"], rule["high"] = amounts[0], amounts[-1]
    elif amounts:
        rule["low"] = amounts[0]
    if "offset" in rule and "low" in rule and rule["offset"] not in amounts and rule["low"] > rule["offset"]:
        pass
    return rule


def _rows(table: Table, label: int | None, value: int) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    seen: set[tuple[str, float]] = set()
    for row in table.rows:
        if value >= len(row):
            continue
        val = _num(row[value]) if not isinstance(row[value], str) else None
        if val is None:
            continue
        name = str(row[label]).strip() if label is not None and label < len(row) and row[label] not in (None, "") else ""
        if not name or name.lower() in {"total", "grand total", "sum"}:
            continue
        key = (name.lower(), val)
        if key in seen:
            continue
        seen.add(key)
        out.append((name, val))
    return out


def apply_rule(val: float, rule: dict) -> float | None:
    low, high = rule.get("low"), rule.get("high")
    if "offset" in rule and low is not None and high is not None and low <= val <= high:
        return val + rule["offset"]
    if "mult" in rule:
        return val * rule["mult"]
    if "offset" in rule:
        return val + rule["offset"]
    return None


def facts(tables: list[Table], ask: str, rule_text: str = "", subject: str = "", max_list: int = 20) -> str:
    """Computed facts for the draft. Empty when nothing numeric matches."""
    if not tables:
        return ""
    subj = set(re.findall(r"[a-z]{3,}", (subject + " " + ask).lower()))
    usable = [t for t in tables if t.rows]
    if not usable:
        return ""
    usable.sort(key=lambda t: -(sum(2 for w in subj if w in t.name.lower()) + min(len(t.rows), 500) / 1000))
    out: list[str] = []
    rule = parse_rule(rule_text)
    for table in usable[:2]:
        label, value = pick_columns(table, ask + " " + rule_text)
        if value is None:
            continue
        rows = _rows(table, label, value)
        if not rows:
            continue
        vals = [v for _n, v in rows]
        col = table.header[value]
        out.append(f'Sheet "{table.name}": {len(rows)} rows by "{table.header[label] if label is not None else "row"}"; '
                   f'"{col}" ranges {fmt_money(min(vals))} to {fmt_money(max(vals))}, total {fmt_money(sum(vals))}.')
        cuts = sorted({c for c in [rule.get("low"), rule.get("high")] if c} | set(money(ask)))
        if cuts:
            low = cuts[0]
            under = sorted([(n, v) for n, v in rows if 0 < v < low], key=lambda item: item[1])
            zero = [n for n, v in rows if v == 0]
            if under:
                listed = ", ".join(f"{n} ({fmt_money(v)})" for n, v in under[:max_list])
                more = f" and {len(under) - max_list} more" if len(under) > max_list else ""
                out.append(f"{len(under)} rows are BELOW {fmt_money(low)} (under the bottom of the band): {listed}{more}.")
            else:
                out.append(f"No rows are under {fmt_money(low)}.")
            if zero:
                out.append(f"{len(zero)} rows have {col} = 0 (likely placeholders): " + ", ".join(zero[:8]) + ".")
            bands = []
            edges = [0.0] + cuts + [float("inf")]
            for lo, hi in zip(edges, edges[1:]):
                band = [(n, v) for n, v in rows if v > 0 and (lo <= v < hi or (hi == cuts[-1] and v == hi))]
                if not band:
                    continue
                band.sort(key=lambda item: (abs(item[1] - lo), item[1]))
                n, v = band[0]
                text = f"{fmt_money(lo)} to {fmt_money(hi) if hi != float('inf') else 'up'}: {len(band)} rows (e.g. {n} at {fmt_money(v)}"
                calc = apply_rule(v, rule) if rule else None
                if calc is not None:
                    text += f" -> rule gives {fmt_money(calc)}"
                bands.append(text + ")")
            if bands:
                out.append("Bands: " + "; ".join(bands) + ".")
            if rule and "offset" in rule and "low" in rule and "high" in rule:
                inband = sorted([(n, v) for n, v in rows if rule["low"] <= v <= rule["high"]], key=lambda item: item[1])
                if inband:
                    n, v = inband[0]
                    out.append(f"WORKED EXAMPLE (rule applied): {n} at {fmt_money(v)} -> {fmt_money(apply_rule(v, rule) or 0)}.")
        if rule:
            pieces = []
            if "offset" in rule and "low" in rule and "high" in rule:
                pieces.append(f"{fmt_money(rule['low'])} to {fmt_money(rule['high'])} gets +{fmt_money(rule['offset'])}")
            if "mult" in rule:
                pieces.append(f"otherwise x{rule['mult']:g}")
            if pieces:
                out.append("RULE IS IN THE NOTES (so it is part of the logic): " + ", ".join(pieces) + ".")
                if "low" in rule:
                    below = [(n, v) for n, v in rows if 0 < v < rule["low"]]
                    if below and "offset" in rule:
                        ex_n, ex_v = below[0]
                        out.append(f"GAP: the rule does not force a {fmt_money(rule['low'])} minimum. The {len(below)} rows below "
                                   f"{fmt_money(rule['low'])} take the 'otherwise' branch (e.g. {ex_n} {fmt_money(ex_v)} -> "
                                   f"{fmt_money(apply_rule(ex_v, rule) or 0)}). If they were bumped to {fmt_money(rule['low'])}, "
                                   f"their level would be {fmt_money(apply_rule(rule['low'], rule) or 0)}.")
        out.extend(_filters(table, ask, label, value))
        top = sorted(rows, key=lambda item: -item[1])[:3]
        out.append("Largest: " + ", ".join(f"{n} ({fmt_money(v)})" for n, v in top) + ".")
    for table in usable[:3]:
        out.extend(_duplicates(table))
    return "\n".join(out)


def _filters(table: Table, ask: str, label: int | None, value: int) -> list[str]:
    """Rows whose category cell is a word in the ask: "orders on inactive accounts" -> Status = Inactive."""
    words = set(re.findall(r"[a-z][a-z-]{2,}", (ask or "").lower()))
    _nums, texts = _cols(table)
    out = []
    for idx in texts:
        values = {str(r[idx]).strip() for r in table.rows if idx < len(r) and r[idx] not in (None, "")}
        if not (2 <= len(values) <= 12):
            continue
        for val in sorted(values):
            if val.lower() in words:
                hit = [r for r in table.rows if idx < len(r) and str(r[idx]).strip() == val]
                total = sum((_num(r[value]) or 0) for r in hit if value < len(r) and not isinstance(r[value], str))
                ids = []
                for r in hit:
                    parts = [str(r[i]) for i in texts if i != idx and i < len(r) and r[i] not in (None, "")]
                    amt = _num(r[value]) if value < len(r) and not isinstance(r[value], str) else None
                    ids.append(" ".join(parts) + (f" {fmt_money(amt)}" if amt is not None else ""))
                out.append(f'{len(hit)} of {len(table.rows)} rows have {table.header[idx]} = {val}; '
                           f'{table.header[value]} total {fmt_money(total)} (${total:,.0f}): ' + "; ".join(ids[:15]) + ".")
    return out


def _duplicates(table: Table) -> list[str]:
    out = []
    for idx, name in enumerate(table.header):
        low = name.lower()
        if not any(k in low for k in ("email", "id", "#", "number", "name", "sku", "order", "account")):
            continue
        counts: dict[str, int] = {}
        for r in table.rows:
            if idx < len(r) and r[idx] not in (None, ""):
                key = str(r[idx]).strip().lower()
                counts[key] = counts.get(key, 0) + 1
        dups = {k: c for k, c in counts.items() if c > 1}
        if dups and len(counts) > 2:
            out.append(f'Sheet "{table.name}" column "{name}": {len(dups)} values appear more than once '
                       f'({sum(c - 1 for c in dups.values())} extra rows): '
                       + ", ".join(f"{k} ({c}x)" for k, c in list(dups.items())[:10])
                       + f". {len(counts)} unique of {sum(counts.values())} rows.")
            break
    if not out and table.rows:
        # Whole-row duplicates
        seen: dict[tuple, int] = {}
        for r in table.rows:
            key = tuple(str(v) for v in r)
            seen[key] = seen.get(key, 0) + 1
        dups = [k for k, c in seen.items() if c > 1]
        if dups:
            out.append(f'Sheet "{table.name}": {len(dups)} rows are exact duplicates.')
    return out
