#!/bin/bash
# Add candle validation to ensemble.py
cd ~/Dev/Deeptrading/backend

# Create backup
cp app/services/ensemble.py app/services/ensemble.py.bak

# Add validation function after imports
cat > /tmp/candle_validation.py << 'EOF'

def _validate_candles(candles: list) -> tuple:
    """Validate candles for anomalies and broken data.
    
    Returns:
        (valid_candles, skipped_count)
    """
    valid = []
    skipped = 0
    for c in candles:
        # Check for zero/negative prices
        if c.open <= 0 or c.close <= 0 or c.high <= 0 or c.low <= 0:
            skipped += 1
            continue
        # Check high >= low
        if c.high < c.low:
            skipped += 1
            continue
        # Check high >= open and high >= close
        if c.high < c.open or c.high < c.close:
            skipped += 1
            continue
        # Check low <= open and low <= close
        if c.low > c.open or c.low > c.close:
            skipped += 1
            continue
        # Check volume >= 0
        if c.volume < 0:
            skipped += 1
            continue
        valid.append(c)
    return valid, skipped

EOF

# Insert validation function after line 20 (after imports)
sed -i '' '20r /tmp/candle_validation.py' app/services/ensemble.py

# Add validation call after line ~1050 (after "мало свечей" check)
# Find the line with "return {"error": "мало свечей""
LINE=$(grep -n 'return {"error": "мало свечей"' app/services/ensemble.py | head -1 | cut -d: -f1)

if [ -n "$LINE" ]; then
    # Insert validation after this line
    sed -i '' "${LINE}a\\
\\
    # --- Validate candles for anomalies ---\\
    candles, skipped_count = _validate_candles(candles)\\
    if skipped_count > 0:\\
        print(f"[ensemble] Skipped {skipped_count} broken candles")\\
    if len(candles) < 120:\\
        return {"error": "мало свечей после валидации", "bars": len(candles)}
" app/services/ensemble.py
fi

echo "Validation added to ensemble.py"
