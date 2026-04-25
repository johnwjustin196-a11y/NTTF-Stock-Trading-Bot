"""Rules ledger.

The EOD reflection LLM proposes structured rules (in addition to the markdown
prose). We persist each rule here and score it against the decisions that
actually fired it — tracking fire_count, hit_count, avg_edge, last_fired.

Rules are NEVER auto-removed. The dashboard surfaces hit rate + fire count +
avg edge so the user can evaluate and remove poorly-performing rules by hand.
This file only *measures*; it does not prune.

Schema (data/rules.json):
  {
    "version": 1,
    "rules": [
      {
        "id": "r_2026-04-21_01",              # stable human-readable id
        "text": "Skip BUYs when breadth < -0.3 and trend is downtrend",
        "condition": "breadth_score < -0.3 and trend == 'downtrend'",
        "action": "SKIP_BUY",
        "rationale": "...",
        "proposed_on": "2026-04-21",
        "regime_when_proposed": "bearish",
        "active": true,                       # user can toggle on dashboard;
                                              # inactive rules are still scored
                                              # but are not sent to the advisor
        "stats": {
          "fire_count": 12,                   # times condition matched a decision
          "hit_count": 8,                     # times the rule's direction was correct
          "follow_count": 7,                  # times the bot acted in line with the rule
          "follow_hit_count": 6,              # hits when following
          "avg_edge": 0.0094,                 # avg realized edge when fired
          "avg_edge_when_followed": 0.0121,
          "last_fired": "2026-04-21"
        }
      }
    ]
  }
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.logger import get_logger

log = get_logger(__name__)


# ------------------------------------------------------------- load / save

def _load() -> dict[str, Any]:
    path = Path(load_config()["paths"]["rules_file"])
    if not path.exists():
        return {"version": 1, "rules": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"rules.json unreadable, starting fresh: {e}")
        return {"version": 1, "rules": []}


def _save(data: dict[str, Any]) -> None:
    path = Path(load_config()["paths"]["rules_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ------------------------------------------------------------- public API

def load_rules(*, active_only: bool = False) -> list[dict]:
    """Return the rules array. Order preserved (oldest first)."""
    rules = _load().get("rules") or []
    if active_only:
        rules = [r for r in rules if r.get("active", True)]
    return rules


def add_proposed_rules(
    rules: list[dict],
    *,
    regime: str = "",
) -> int:
    """Persist new LLM-proposed rules. De-dupes by normalized text.

    Returns the number of genuinely new rules added (duplicates are ignored).
    """
    if not rules:
        return 0
    data = _load()
    existing = data.get("rules") or []
    existing_texts = {_normalize_text(r.get("text", "")) for r in existing}
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # Seed id counter from today's existing proposals so ids stay stable across runs
    same_day_count = sum(
        1 for r in existing if (r.get("proposed_on") or "") == today
    )
    added = 0
    for r in rules:
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        norm = _normalize_text(text)
        if norm in existing_texts:
            continue
        active_rules = [r for r in existing if r.get("active", True)]
        if any(_keyword_overlap(text, r.get("text", "")) > 0.60 for r in active_rules):
            continue
        same_day_count += 1
        rule = {
            "id": f"r_{today}_{same_day_count:02d}",
            "text": text[:200],
            "condition": str(r.get("condition") or "")[:300],
            "action": str(r.get("action") or "PREFER_HOLD").upper()[:32],
            "rationale": str(r.get("rationale") or "")[:300],
            "proposed_on": today,
            "regime_when_proposed": regime or "",
            "active": True,
            "stats": {
                "fire_count": 0,
                "hit_count": 0,
                "follow_count": 0,
                "follow_hit_count": 0,
                "edge_sum": 0.0,
                "edge_sum_when_followed": 0.0,
                "avg_edge": 0.0,
                "avg_edge_when_followed": 0.0,
                "last_fired": None,
            },
        }
        existing.append(rule)
        existing_texts.add(norm)
        added += 1
    data["rules"] = existing
    _save(data)
    return added


def score_rules_against_outcomes(outcomes: list[dict]) -> int:
    """For each rule, check today's outcomes: did the condition match? Was the
    rule's direction correct? Did the bot actually follow it?

    Updates the in-file stats and returns the count of rules with new fires.
    """
    if not outcomes:
        return 0
    data = _load()
    rules = data.get("rules") or []
    if not rules:
        return 0

    today = outcomes[0].get("date") or datetime.utcnow().strftime("%Y-%m-%d")
    rules_updated = 0

    for rule in rules:
        action = (rule.get("action") or "").upper()
        fires = 0
        hits = 0
        followed = 0
        followed_hits = 0
        edge_sum = 0.0
        followed_edge_sum = 0.0
        for o in outcomes:
            if not _rule_matches(rule, o):
                continue
            out = o.get("outcome") or {}
            edge = float(out.get("edge") or 0.0)
            hit = out.get("hit")
            fires += 1
            edge_sum += edge
            if hit is True:
                hits += 1
            # "Followed" = the bot's action agrees with what the rule would suggest.
            # A rule is "right" when the opposite outcome would have been worse.
            # We infer compliance from the decision action:
            #   SKIP_BUY / PREFER_HOLD: followed if action == HOLD
            #   FORCE_CLOSE           : followed if action == CLOSE
            #   REDUCE_SIZE           : followed if executed and action in (BUY,)
            #                            with quality in ("weak","normal")
            #   SKIP_SELL             : followed if action != CLOSE
            if _rule_followed(action, o):
                followed += 1
                followed_edge_sum += edge
                if hit is True:
                    followed_hits += 1
        if fires == 0:
            continue
        stats = rule.setdefault("stats", {})
        stats["fire_count"] = int(stats.get("fire_count", 0)) + fires
        stats["hit_count"] = int(stats.get("hit_count", 0)) + hits
        stats["follow_count"] = int(stats.get("follow_count", 0)) + followed
        stats["follow_hit_count"] = int(stats.get("follow_hit_count", 0)) + followed_hits
        stats["edge_sum"] = float(stats.get("edge_sum", 0.0)) + edge_sum
        stats["edge_sum_when_followed"] = (
            float(stats.get("edge_sum_when_followed", 0.0)) + followed_edge_sum
        )
        fc = max(stats["fire_count"], 1)
        fol = max(stats["follow_count"], 1)
        stats["avg_edge"] = float(stats["edge_sum"]) / fc
        stats["avg_edge_when_followed"] = (
            float(stats["edge_sum_when_followed"]) / fol
            if stats["follow_count"] else 0.0
        )
        stats["last_fired"] = today
        rules_updated += 1

    data["rules"] = rules
    _save(data)
    return rules_updated


def flag_weak_rules_for_retirement() -> list[str]:
    """Weekly pass: flag rules with poor performance for user review on dashboard.

    Criteria:
      - fire_count >= 30
      - hit_rate < 0.38
      - Not already flagged this week

    For each qualifying rule, fires an LLM call to write a 2-3 sentence
    diagnosis and optionally propose a replacement rule. Stores the result
    as rule["retirement_flag"] = {reason, diagnosis, flagged_on, suggested_replacement}.

    NEVER disables or removes rules automatically. The user sees flagged rules
    highlighted on the dashboard and clicks "Disable" or "Keep" manually.

    Returns list of rule IDs that were newly flagged.
    """
    from datetime import datetime
    from ..utils.llm_client import chat, llm_available, extract_json_object

    data = _load()
    rules = data.get("rules") or []
    today = datetime.utcnow().strftime("%Y-%m-%d")
    flagged_ids: list[str] = []

    for rule in rules:
        if not rule.get("active", True):
            continue
        stats = rule.get("stats") or {}
        fires = int(stats.get("fire_count", 0))
        hits = int(stats.get("hit_count", 0))
        if fires < 30:
            continue
        hit_rate = hits / fires
        if hit_rate >= 0.38:
            continue

        # Check if already flagged recently (within 7 days)
        existing_flag = rule.get("retirement_flag") or {}
        existing_date = existing_flag.get("flagged_on", "")
        if existing_date and (datetime.utcnow() - datetime.fromisoformat(existing_date)).days < 7:
            continue

        avg_edge = float(stats.get("avg_edge", 0.0) or 0.0)
        diagnosis = (
            f"Rule has {fires} fires with only {hit_rate:.0%} hit rate "
            f"(avg edge {avg_edge:+.2%}). Performance is below the 38% threshold."
        )
        suggested_replacement = None

        ok, _ = llm_available()
        if ok:
            try:
                prompt = (
                    f"A trading rule is underperforming. Diagnose why and optionally suggest a replacement.\n\n"
                    f"Rule: {rule.get('text', '')}\n"
                    f"Condition: {rule.get('condition', '')}\n"
                    f"Action: {rule.get('action', '')}\n"
                    f"Rationale: {rule.get('rationale', '')}\n"
                    f"Stats: {fires} fires | {hit_rate:.0%} hit rate | avg edge {avg_edge:+.2%}\n\n"
                    f"Return JSON with:\n"
                    f'  "diagnosis": 2-3 sentences on why this rule is failing\n'
                    f'  "suggested_replacement": null or a brief replacement rule text (<=100 chars)\n\n'
                    f"ONE JSON object only. Start with {{ end with }}."
                )
                text = chat(prompt=prompt, system=None, max_tokens=250, temperature=0.3, tag="rules")
                parsed = extract_json_object(text)
                if parsed.get("diagnosis"):
                    diagnosis = str(parsed["diagnosis"])[:500]
                if parsed.get("suggested_replacement"):
                    suggested_replacement = str(parsed["suggested_replacement"])[:200]
            except Exception:
                pass

        rule["retirement_flag"] = {
            "reason": f"{fires} fires, {hit_rate:.0%} hit rate (threshold: 38%)",
            "diagnosis": diagnosis,
            "flagged_on": today,
            "suggested_replacement": suggested_replacement,
        }
        flagged_ids.append(rule.get("id", ""))
        log.info(
            f"[rules] flagged '{rule.get('id')}' for retirement review: "
            f"{fires} fires, {hit_rate:.0%} hit rate"
        )

    if flagged_ids:
        data["rules"] = rules
        _save(data)

    return flagged_ids


def set_rule_active(rule_id: str, active: bool) -> bool:
    """Toggle a rule's 'active' flag — used by the dashboard when the user
    decides to disable a poorly-performing rule. The rule stays in the file
    for the historical record; it just isn't sent to the advisor anymore.
    """
    data = _load()
    for rule in data.get("rules") or []:
        if rule.get("id") == rule_id:
            rule["active"] = bool(active)
            _save(data)
            return True
    return False


def delete_rule(rule_id: str) -> bool:
    """Permanently remove a rule. Called only from the dashboard UI when the
    user explicitly clicks Delete — we NEVER auto-prune."""
    data = _load()
    rules = data.get("rules") or []
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return False
    data["rules"] = new_rules
    _save(data)
    return True


def get_promoted_rules(*, regime: str | None = None) -> list[dict]:
    """Return rules promoted to hard-constraint status.

    Promotion criteria (checked at rule-scoring time, never auto-applied):
      - active == True
      - fire_count >= 25
      - hit_rate >= 0.72

    These are enforced in the decision engine BEFORE the LLM call. A promoted
    rule is also still sent to the advisor as advisory context.

    Regime filtering: if regime is given, prefer matching rules but include
    universal ones (no regime_when_proposed set).
    """
    rules = load_rules(active_only=True)
    promoted: list[dict] = []
    for r in rules:
        stats = r.get("stats") or {}
        fires = int(stats.get("fire_count", 0))
        hits = int(stats.get("hit_count", 0))
        if fires < 25:
            continue
        hit_rate = hits / fires
        if hit_rate < 0.72:
            continue
        r_regime = (r.get("regime_when_proposed") or "").lower()
        current = (regime or "").lower()
        if r_regime and current and r_regime != current:
            continue  # skip regime-mismatched promoted rules
        promoted.append(r)
    return promoted


def check_promoted_rules(decision: dict, regime: str | None = None) -> list[str]:
    """Evaluate all promoted rules against a candidate decision.

    Returns a list of block reasons (one per triggered rule). Empty list = no block.
    Only BUY-blocking actions are enforced here; FORCE_CLOSE / PREFER_HOLD are
    advisory and returned for the caller to handle.

    Matching reuses _rule_matches() so the logic is identical to scoring.
    """
    promoted = get_promoted_rules(regime=regime)
    if not promoted:
        return []
    blocks: list[str] = []
    for rule in promoted:
        action = (rule.get("action") or "").upper()
        if action not in ("SKIP_BUY", "PREFER_HOLD", "FORCE_CLOSE", "REDUCE_SIZE"):
            continue
        if _rule_matches(rule, decision):
            rule_text = rule.get("text", "")[:120]
            stats = rule.get("stats") or {}
            fires = int(stats.get("fire_count", 0))
            hits = int(stats.get("hit_count", 0))
            hr = hits / fires if fires else 0.0
            blocks.append(
                f"[promoted rule {action} | {hr:.0%} hit rate / {fires} fires] {rule_text}"
            )
    return blocks


def rules_for_prompt(
    *,
    regime: str | None,
    limit: int = 12,
) -> list[dict]:
    """Return the set of active rules to inject into the advisor prompt.

    Selection logic:
      1. Prefer active rules whose regime matches the current regime, sorted
         by rule age descending (newer rules tend to be most relevant).
      2. If fewer than `limit` rules match, backfill with other active rules.
    Empty list if there are no active rules at all.
    """
    active = [r for r in load_rules(active_only=True)]
    if not active:
        return []
    matching: list[dict] = []
    other: list[dict] = []
    for r in active:
        if regime and r.get("regime_when_proposed") == regime:
            matching.append(r)
        else:
            other.append(r)
    matching.sort(key=lambda r: r.get("proposed_on") or "", reverse=True)
    other.sort(key=lambda r: r.get("proposed_on") or "", reverse=True)
    combined = matching + other
    return combined[:limit]


# ------------------------------------------------------------- matching

def _rule_matches(rule: dict, outcome: dict) -> bool:
    """Very lightweight condition matching.

    We don't try to evaluate arbitrary Python expressions from the LLM — that
    would be a footgun. Instead we parse a few well-known keywords out of the
    condition string and check them against the outcome fields. Anything we
    can't interpret falls back to 'does the regime match'. This is good enough
    to measure whether a rule would have *been relevant* to a given decision,
    which is what the dashboard cares about.
    """
    cond = (rule.get("condition") or "").lower()
    regime = (outcome.get("regime") or "").lower()
    trend = (outcome.get("trend") or "").lower()
    quality = (outcome.get("quality") or "").lower()
    action = (outcome.get("action") or "").upper()
    breadth_score = _signal(outcome, "breadth")

    # If the condition mentions a specific regime word, require it to match.
    for w in ("bullish", "bearish", "volatile", "neutral"):
        if w in cond and w not in regime:
            return False
    # Trend mentions
    for w in ("uptrend", "downtrend", "sideways"):
        if w in cond and w not in trend:
            return False
    # Quality mentions
    for w in ("strong", "weak", "normal"):
        if w in cond and w not in quality:
            return False
    # Breadth threshold keywords like "breadth < -0.3" / "breadth > 0.3"
    if "breadth" in cond:
        thr = _extract_float_after(cond, "breadth")
        if thr is not None and breadth_score is not None:
            if "<" in cond and not (breadth_score < thr):
                return False
            if ">" in cond and not (breadth_score > thr):
                return False
    # Action-mention gate: if condition mentions BUY/CLOSE/HOLD explicitly,
    # only count decisions with that action.
    for w in ("buy", "close", "sell", "hold"):
        if w in cond.split():
            if action != w.upper():
                return False
    # If no keyword in condition matched at all, default to 'regime-tagged'
    # match: regime_when_proposed must equal current regime.
    if not any(k in cond for k in ("bullish", "bearish", "volatile", "neutral",
                                    "uptrend", "downtrend", "sideways",
                                    "strong", "weak", "normal", "breadth",
                                    "buy", "close", "sell", "hold")):
        rule_regime = (rule.get("regime_when_proposed") or "").lower()
        if rule_regime and rule_regime != regime:
            return False
    return True


def _rule_followed(action_code: str, outcome: dict) -> bool:
    act = (outcome.get("action") or "").upper()
    if action_code == "SKIP_BUY":
        return act != "BUY"
    if action_code == "PREFER_HOLD":
        return act == "HOLD"
    if action_code == "FORCE_CLOSE":
        return act == "CLOSE"
    if action_code == "SKIP_SELL":
        return act != "CLOSE"
    if action_code == "REDUCE_SIZE":
        # Can't see share count from outcome row; best proxy: quality tag
        return (outcome.get("quality") or "").lower() in ("weak", "normal")
    return False


def _signal(outcome: dict, key: str) -> float | None:
    v = (outcome.get("signals") or {}).get(key)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _extract_float_after(s: str, keyword: str) -> float | None:
    """Given 'breadth < -0.3' extract -0.3. Used only for the few inline
    threshold conditions we understand."""
    idx = s.find(keyword)
    if idx < 0:
        return None
    tail = s[idx + len(keyword):]
    # Find the first number-like substring
    m = re.search(r"-?\d+(?:\.\d+)?", tail)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _keyword_overlap(text_a, text_b):
    _stopwords = {"the", "a", "is", "if", "and", "or", "when", "in", "on", "of", "to", "for", "not", "at", "by", "be"}
    def _tok(t):
        return set(re.findall(r"\w+", t.lower())) - _stopwords
    t1, t2 = _tok(text_a), _tok(text_b)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


def _normalize_text(s: str) -> str:
    return " ".join((s or "").lower().split())
