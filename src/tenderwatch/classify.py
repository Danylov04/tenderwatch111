"""Прозора класифікація: relevant / review / noise + причини, теги, категорія.

Жодних «чорних скриньок»: кожен бал має пояснення в `reasons`, правила лежать у rules.toml.
LLM тут не потрібен; сірa зона (`review`) віддається людині (або LLM-агенту поверх БД).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Rules
from .models import Notice
from .util import clean

BODY_CAP = 6.0


@dataclass
class Classification:
    relevance: str
    score: float
    reasons: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = ""
    strong: bool = False           # є хоч один сильний сигнал (сильний CPV або сильне слово в назві)


def _compile(rules: Rules):
    cache = getattr(rules, "_compiled", None)
    if cache is not None:
        return cache
    cl = rules.raw["classify"]
    pos = [(re.compile(r["pattern"], re.I), float(r["weight"]), r.get("tag", "")) for r in cl.get("positive", [])]
    neg = [(re.compile(r["pattern"], re.I), float(r["weight"]), r.get("reason", "")) for r in cl.get("negative", [])]
    rules._compiled = (pos, neg)  # type: ignore[attr-defined]
    return pos, neg


def _code8(code: str) -> str:
    return re.sub(r"\D", "", code or "")[:8]


def classify(n: Notice, rules: Rules) -> Classification:
    pos, neg = _compile(rules)
    cl = rules.raw["classify"]
    body_factor = float(cl.get("body_factor", 0.7))
    rel_thr = float(cl.get("relevant_threshold", 3.0))
    rev_thr = float(cl.get("review_threshold", 1.0))

    title = clean(n.title)
    body = clean(" ".join([n.description or "", n.body_text or ""]))
    reasons: list[str] = []
    tags: list[str] = []
    score = 0.0
    strong = False

    # 1) CPV
    codes = {_code8(c) for c in n.cpv}
    for c in rules.cpv_strong:
        if c in codes:
            score += 5.0
            strong = True
            tags.append("shelter")
            reasons.append(f"+5.0 CPV {c}")
    if not strong:
        for c in rules.cpv_medium:
            if c in codes:
                score += 1.5
                reasons.append(f"+1.5 CPV {c} (батьківський)")
                break

    # 2) назва
    title_hit_rules: set[int] = set()
    for i, (rx, w, tag) in enumerate(pos):
        m = rx.search(title)
        if m:
            title_hit_rules.add(i)
            score += w
            if tag:
                tags.append(tag)
            reasons.append(f"+{w:g} «{m.group(0)}» (назва)")
            if w >= 3.5:
                strong = True

    # 3) тіло (лише те, чого нема в назві), із стелею
    body_score = 0.0
    if body:
        for i, (rx, w, tag) in enumerate(pos):
            if i in title_hit_rules:
                continue
            m = rx.search(body)
            if m and w >= 1.5:  # слабкі слова в тілі шуму не додають
                add = round(w * body_factor, 2)
                if body_score + add > BODY_CAP:
                    add = max(0.0, BODY_CAP - body_score)
                body_score += add
                if tag:
                    tags.append(tag)
                reasons.append(f"+{add:g} «{m.group(0)}» (тіло)")
                if w >= 3.5:
                    strong = True
        score += body_score

    # 4) шум
    penalty = 0.0
    for rx, w, reason in neg:
        if rx.search(title):
            penalty += w
            reasons.append(f"-{w:g} {reason}")
    if any(c in codes for c in rules.cpv_strong):
        penalty *= 0.5  # сильний CPV частково перебиває шум у назві
    score -= penalty

    score = round(score, 2)
    if score >= rel_thr and strong:
        relevance = "relevant"
    elif score >= rev_thr:
        relevance = "review"
    else:
        relevance = "noise"
    if score >= rel_thr and not strong:
        relevance = "review"   # набір слабких ознак сам по собі не «relevant»

    # 5) тип робіт і категорія
    tags = _dedup(tags)
    low = title.lower()
    if re.search(r"dokumentacj|projektow|nadzór|nadzor|ekspertyz|koncepcj|opracowani", low) and not re.search(
        r"zaprojektuj|projektuj i buduj|projektowanie i wykonanie|wykonanie rob|roboty budowlane", low
    ):
        tags.append("design_or_service")
    if n.kind == "supplies" or re.search(r"\bdostaw\w*|\bzakup\b", low):
        tags.append("equipment")
        # обладнання/закупівлі без сильного сигналу — не будівництво укриттів: лише на перевірку
        if relevance == "relevant" and not any(c in codes for c in rules.cpv_strong) and "shelter" not in tags:
            relevance = "review"
            reasons.append("закупівля обладнання без ознак будівництва укриття")

    category = _category(tags)
    return Classification(relevance, score, reasons, _dedup(tags), category, strong)


def _dedup(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _category(tags: list[str]) -> str:
    t = set(tags)
    if "shelter" in t and "modular" in t:
        return "modular_shelter"
    if "shelter" in t:
        return "shelter"
    if "dual_use" in t:
        return "dual_use"
    if "military" in t:
        return "military"
    if "civil_defense" in t:
        return "infrastructure"
    return "other"


def needs_body_scan(n: Notice, title_verdict: Classification, rules: Rules) -> bool:
    """Чи варто завантажувати тіло: назва не дала «relevant», але це будівельний/проєктний CPV."""
    if title_verdict.relevance == "relevant":
        return False
    if n.lifecycle not in ("open", "unknown", "planned"):
        return False
    prefixes = tuple(rules.construction_prefixes)
    return any(_code8(c).startswith(prefixes) for c in n.cpv) or not n.cpv
