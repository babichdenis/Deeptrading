#!/bin/bash
cd ~/Dev/Deeptrading/backend

# Add validation call after line 1073
sed -i '' '1073a\
\
    # --- Validate candles for anomalies ---\
    candles, skipped_count = _validate_candles(candles)\
    if skipped_count > 0:\
        print(f"[ensemble] Skipped {skipped_count} broken candles")\
    if len(candles) < 120:\
        return {"error": "мало свечей после валидации", "bars": len(candles)}
' app/services/ensemble.py

echo "Validation call added"
