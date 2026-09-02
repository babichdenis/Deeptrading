# Task: Ensemble vote behavior at entry/exit — RE-RUN (continuous strength + forward bars)

## Goal

Re-implement B5b with correct continuous strength and forward-bar dynamics.

Do NOT change:
- EngineRunner
- entry/exit logic
- quorum / cooldown / session policy
- CostModel / sizing / ML
- actual trade results

Use only:
- canonical baseline runs (July 2026)
- point-in-time function signals

## Critical fixes vs previous run

1. **Direction must not degenerate:**
   - For each function at each bar, compute:
     - vote_direction ∈ {long, short, neutral}
     - based on function's actual signal direction (not always neutral).

2. **Strength must be continuous:**
   - For each function at each bar, compute:
     - vote_strength ∈ [0, 1] or ℝ (e.g., distance-to-threshold, z-score, normalized metric).
   - Do NOT use binary (1/0) strength.

3. **Forward-bar dynamics:**
   - For each entry:
     - compute strength at t0, t0+1, t0+2, t0+3, t0+5 bars.
     - separate winners (net>0) vs losers (net≤0).
     - compute mean/median strength trajectory for winners vs losers.
   - For each exit:
     - compute strength at tE, tE−1, tE−3, tE−5 bars.
     - detect foreshadow patterns for signal_exit.

## Data

Input:
- trades.csv (July 2026, 357 trades)
- entry_intents.csv
- function signals per bar (point-in-time)

## Method

For each trade and each function:

1. At entry bar (t0):
   - record vote_direction (long/short/neutral)
   - record vote_strength (continuous)

2. For forward bars (t0+1, t0+2, t0+3, t0+5):
   - re-evaluate function signal
   - record vote_strength (continuous)
   - mark if signal flipped direction

3. At exit bar (tE) and lookback (tE−1, tE−3, tE−5):
   - record vote_direction and vote_strength
   - detect if function showed weakening/contrary signal before signal_exit

4. Aggregate:
   - consensus strength at t0 vs outcome (win/loss)
   - strength dynamics t0..t0+5 for winners vs losers
   - foreshadow patterns for signal_exit

## Output

- reports/{run_id}/entry_exit_vote_behavior.json
  - per-function direction@t0, tE
  - consensus-strength vs win/loss
  - strength_dynamics_winners_vs_losers (t0+0..t0+5)
  - signal_exit_foreshadow (tE−5..tE)
- reports/{run_id}/entry_exit_vote_audit.csv
  - trade_id, figi, side, exit_type, function
  - rel_bar (0,+1,+2,+3,+5 / −5,−3,−1)
  - vote_direction, vote_strength, outcome

## Tests

- direction not degenerated (long/short/neutral present)
- strength is continuous (not binary)
- forward-bar dynamics computed (no nulls)
- all values point-in-time
- no oracle data

## Conclusion

One of:
- SUPPORTED_FOR_FUTURE_EXPERIMENT
- INCONCLUSIVE
- REJECTED
