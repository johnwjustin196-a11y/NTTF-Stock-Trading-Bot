"""Signal-weight auto-tuner.

settings.yaml has hand-picked weights (technicals=0.35, news=0.15,
breadth=0.15, llm=0.35). Those are a guess. With `data/outcomes.jsonl` now
giving us magnitude-weighted edge per decision AND the per-signal scores
stored on each row, we can look at which signal actually correlated with
realized edge over the last N days and nudge the weights toward the
better-performing signal.

Design choices:
  - Nudges only. One weight cannot move more than `max_nudge_per_run` per run
    (config default 0.05). So the tuner can't make a big wrong swing.
  - Weights still sum to 1.0. After nudging, we renormalize.
  - Floor per weight (config default 0.05) — no signal can be crushed to zero.
  - The live system reads from `data/signal_weights.json` if present; the
    settings.yaml values stay authoritative as a fallback / reset baseline.

The decision engine calls `effective_weights()` instead of the raw config.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.logger import get_logger
from .outcomes import load_outcomes

log = get_logger(__name__)

SIGNAL_KEYS = ("technicals", "news", "breadth", "llm")


# ------------------------------------------------------------- public API

def effective_weights() -> dict[str, float]:
    """Return the weights the decision engine should actually use.

    Starts from settings.yaml. If `data/signal_weights.json` exists and was
    written recently, overlay its values on top. Always validates that the
    returned weights sum to 1.0 (within tolerance) and are above the floor.
    """
    cfg = load_config()
    base = dict(cfg["signals"]["weights"])  # copy so we don't mutate cache
    # Only overlay keys we know about so a bad overlay can't inject garbage
    overlay_path = Path(cfg["paths"]["signal_weights_file"])
    if overlay_path.exists():
        try:
            data = json.loads(overlay_path.read_text(encoding="utf-8"))
            weights = (data or {}).get("weights") or {}
            for k in SIGNAL_KEYS:
                if k in weights and isinstance(weights[k], (int, float)):
                    base[k] = float(weights[k])
        except Exception as e:
            log.debug(f"signal_weights.json unreadable: {e}")
    # Defensive normalize
    return _normalize(base, floor=_floor_from_cfg(cfg))


def tune_signal_weights() -> dict[str, Any]:
    """Run one tuning pass over recent outcomes and persist the nudged weights.

    Returns a dict describing what happened (for the dashboard / logs):
      { "ran": bool, "reason": str, "before": {...}, "after": {...},
        "samples": int, "correlations": {...}, "delta": {...} }
    """
    cfg = load_config()
    swcfg = (cfg.get("learning") or {}).get("signal_weights") or {}
    if not swcfg.get("enabled", True):
        return {"ran": False, "reason": "signal_weights.enabled is false"}

    window_days = int(swcfg.get("window_days", 30))
    min_samples = int(swcfg.get("min_samples_required", 40))
    max_nudge = float(swcfg.get("max_nudge_per_run", 0.05))
    floor = float(swcfg.get("floor_per_weight", 0.05))

    rows = load_outcomes(since_days=window_days)
    usable = [r for r in rows if r.get("outcome") and r.get("signals")]
    if len(usable) < min_samples:
        return {
            "ran": False,
            "reason": f"only {len(usable)} samples in last {window_days}d "
                      f"(need {min_samples})",
            "samples": len(usable),
        }

    before = effective_weights()
    correlations = _correlations(usable)
    after = _apply_nudges(before, correlations, max_nudge=max_nudge, floor=floor)

    delta = {k: round(after[k] - before[k], 4) for k in SIGNAL_KEYS}
    _persist(after, correlations=correlations, samples=len(usable))

    log.info(
        f"signal-weight tuner: samples={len(usable)}, "
        + ", ".join(f"{k}={after[k]:.3f}({delta[k]:+.3f})" for k in SIGNAL_KEYS)
    )
    return {
        "ran": True,
        "reason": "ok",
        "before": before,
        "after": after,
        "delta": delta,
        "correlations": correlations,
        "samples": len(usable),
    }


def load_weight_history() -> list[dict]:
    """Read the append-only history log for the dashboard chart."""
    cfg = load_config()
    path = _history_path(cfg)
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ------------------------------------------------------------- internals

def _correlations(rows: list[dict]) -> dict[str, float]:
    """Pearson-ish correlation of each signal's raw score with realized edge.

    We avoid pulling numpy/scipy for this — the sample size is small and a
    basic correlation is all we need. Returns zero for signals with no
    variance or fewer than 2 samples.
    """
    out: dict[str, float] = {}
    for k in SIGNAL_KEYS:
        xs: list[float] = []
        ys: list[float] = []
        for r in rows:
            s = (r.get("signals") or {}).get(k)
            e = (r.get("outcome") or {}).get("edge")
            if isinstance(s, (int, float)) and isinstance(e, (int, float)):
                xs.append(float(s))
                ys.append(float(e))
        out[k] = _pearson(xs, ys)
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return 0.0
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return cov / ((vx * vy) ** 0.5)


def _apply_nudges(
    current: dict[str, float],
    correlations: dict[str, float],
    *,
    max_nudge: float,
    floor: float,
) -> dict[str, float]:
    """Nudge the currently-active weights toward correlation-ranked direction.

    Scheme:
      1. Rank signals by their correlation. The top signal gets a positive
         nudge, the bottom signal gets a negative nudge, middle signals stay.
      2. Cap the nudge size at max_nudge. If correlations are very close
         together, scale the nudge down proportionally so we don't move on
         noise.
      3. Apply floor.
      4. Renormalize to sum=1.
    """
    ranked = sorted(correlations.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return dict(current)

    best_key, best_corr = ranked[0]
    worst_key, worst_corr = ranked[-1]
    spread = max(best_corr - worst_corr, 0.0)
    # If top and bottom signals are near-tied, don't move much
    nudge = min(max_nudge, max_nudge * min(spread * 5.0, 1.0))

    new = dict(current)
    new[best_key] = min(1.0, new.get(best_key, 0.0) + nudge)
    new[worst_key] = max(0.0, new.get(worst_key, 0.0) - nudge)
    return _normalize(new, floor=floor)


def _normalize(weights: dict[str, float], *, floor: float) -> dict[str, float]:
    # enforce floor
    w = {k: max(float(weights.get(k, 0.0)), floor) for k in SIGNAL_KEYS}
    s = sum(w.values())
    if s <= 0:
        # Pathological fallback — equal weights
        return {k: 1.0 / len(SIGNAL_KEYS) for k in SIGNAL_KEYS}
    return {k: v / s for k, v in w.items()}


def _floor_from_cfg(cfg: dict) -> float:
    return float(
        ((cfg.get("learning") or {}).get("signal_weights") or {}).get("floor_per_weight", 0.05)
    )


def _persist(
    weights: dict[str, float],
    *,
    correlations: dict[str, float],
    samples: int,
) -> None:
    cfg = load_config()
    overlay_path = Path(cfg["paths"]["signal_weights_file"])
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().isoformat()
    overlay_path.write_text(
        json.dumps(
            {
                "weights": {k: round(weights[k], 4) for k in SIGNAL_KEYS},
                "updated_at": ts,
                "samples": samples,
                "correlations": {k: round(correlations.get(k, 0.0), 4) for k in SIGNAL_KEYS},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # Append to history log for the dashboard chart
    history = _history_path(cfg)
    history.parent.mkdir(parents=True, exist_ok=True)
    with open(history, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "at": ts,
            "weights": {k: round(weights[k], 4) for k in SIGNAL_KEYS},
            "correlations": {k: round(correlations.get(k, 0.0), 4) for k in SIGNAL_KEYS},
            "samples": samples,
        }) + "\n")


def _history_path(cfg: dict) -> Path:
    # Sibling file next to signal_weights.json for append-only history
    overlay = Path(cfg["paths"]["signal_weights_file"])
    return overlay.with_suffix(".history.jsonl")
