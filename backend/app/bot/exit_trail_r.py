"""R-based trailing override (env-гейт).

Env:
  TRAIL_R_ACT  — активация трейлинга в R (напр. 1.5)
  TRAIL_R_DIST — дистанция трейла от пика в R (напр. 0.75)

При заданных обеих переменных возвращает kwargs для AtrStopPolicy, включающие
R-режим активации (trail_activation_comm_mult=None) и дистанцию в R.
Иначе — пустой dict (поведение не меняется).
"""
import os


def r_trail_kwargs() -> dict:
    act = (os.environ.get("TRAIL_R_ACT") or "").strip()
    dist = (os.environ.get("TRAIL_R_DIST") or "").strip()
    if not act or not dist:
        return {}
    try:
        a = float(act)
        d = float(dist)
    except ValueError:
        return {}
    if a <= 0.0 or d <= 0.0:
        return {}
    return {
        "trail_activation_comm_mult": None,
        "trail_activation_r": a,
        "trail_distance_r": d,
        "trail_compress_r": 0.0,
        "trail_min_atr": 0.0,
        "trail_vol_boost": 0.0,
    }
