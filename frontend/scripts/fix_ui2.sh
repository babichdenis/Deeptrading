#!/bin/bash
cd ~/Dev/Deeptrading/frontend

# 1. Remove sidebar toggle button
python3 -c "
import re
with open('index.html', 'r') as f:
    content = f.read()
# Remove sidebar-foot div
content = re.sub(r'<div class=\"sidebar-foot\">.*?</div>', '', content, flags=re.DOTALL)
with open('index.html', 'w') as f:
    f.write(content)
print('Removed sidebar-foot')
"

# 2. Remove "Заявки (lifecycle)" section
python3 -c "
import re
with open('index.html', 'r') as f:
    content = f.read()
# Remove h3 and table for orders
content = re.sub(r'<h3 class=\"pg-h3\">Заявки \(lifecycle\)</h3>.*?</table>', '', content, flags=re.DOTALL)
with open('index.html', 'w') as f:
    f.write(content)
print('Removed orders section')
"

# 3. Fix positions table header
python3 -c "
with open('index.html', 'r') as f:
    content = f.read()
old = '<thead><tr><th>Акция</th><th>Side</th><th>Qty</th><th>Вход</th><th>Текущая</th><th>P&L</th><th>Вход время</th><th>SL</th><th>TP</th></tr></thead>'
new = '<thead><tr><th>Вход время</th><th>Акция</th><th>Side</th><th>Qty</th><th>Вход</th><th>Текущая</th><th>P&L</th><th>SL</th><th>TP</th><th></th></tr></thead>'
content = content.replace(old, new)
with open('index.html', 'w') as f:
    f.write(content)
print('Fixed positions header')
"

# 4. Remove stop button from HTML
python3 -c "
with open('index.html', 'r') as f:
    content = f.read()
content = content.replace('<button class=\"btn-danger\" id=\"btn-bot-stopui\">■ Остановить</button>\n', '')
with open('index.html', 'w') as f:
    f.write(content)
print('Removed stop button')
"

echo "All HTML changes done"
