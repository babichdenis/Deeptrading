def update_strategy_history(buffer, candle):
    """Keep a unique causal prefix ending at the delivered closed candle."""
    visible = {bar.ts: bar for bar in buffer if bar.ts <= candle.ts}
    visible[candle.ts] = candle
    ordered = [visible[stamp] for stamp in sorted(visible)]
    buffer.clear()
    buffer.extend(ordered)
