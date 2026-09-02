#!/bin/bash
# Fix bot UI - all changes
cd ~/Dev/Deeptrading/frontend

# Backup files
cp index.html index.html.bak
cp src/bot.ts src/bot.ts.bak

# 1. Remove sidebar toggle button (btn-bot-toggle)
sed -i '' '/<div class="sidebar-foot">/,/<\/div>/d' index.html

# 2. Remove "Заявки (lifecycle)" section (h3 + table)
sed -i '' '/<h3 class="pg-h3">Заявки (lifecycle)<\/h3>/,/<\/table>/d' index.html

# 3. Reorder positions table header: move "Вход время" to first, add action column
sed -i '' 's|<thead><tr><th>Акция</th><th>Side</th><th>Qty</th><th>Вход</th><th>Текущая</th><th>P&L</th><th>Вход время</th><th>SL</th><th>TP</th></tr></thead>|<thead><tr><th>Вход время</th><th>Акция</th><th>Side</th><th>Qty</th><th>Вход</th><th>Текущая</th><th>P&L</th><th>SL</th><th>TP</th><th></th></tr></thead>|' index.html

# 4. Remove the separate stop button from HTML (keep only start button)
sed -i '' '/<button class="btn-danger" id="btn-bot-stopui">/d' index.html

echo "HTML changes done"
