#!/bin/bash
cd ~/Dev/Deeptrading/frontend

# Add CSS for sell/buy buttons at the end of style.css
cat >> src/style.css << 'EOF'

/* Sell/Buy buttons for positions */
.btn-sell {
  background: var(--down);
  color: white;
  border: none;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}
.btn-sell:hover {
  background: #d32f2f;
}
.btn-buy {
  background: var(--up);
  color: white;
  border: none;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}
.btn-buy:hover {
  background: #1b8a6b;
}
.btn-sm {
  font-size: 11px;
  padding: 2px 6px;
}

/* Stop button style */
.btn-bot-start.stop {
  background: var(--down);
}
EOF

echo "CSS added"
