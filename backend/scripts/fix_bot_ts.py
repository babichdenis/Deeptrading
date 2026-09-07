import re

path = "/Users/Denis/Dev/Deeptrading/frontend/src/bot.ts"
with open(path) as f:
    content = f.read()

# Fix open() handler - broken "$(" patterns became "\"
# Lines like: (\ as HTMLInputElement).value = String(cfg.commission);
# Should be: ($("bs-commission") as HTMLInputElement).value = String(cfg.commission);

fixes = [
    # open() handler
    ('(\\ as HTMLInputElement).value = String(cfg.commission);', '($("bs-commission") as HTMLInputElement).value = String(cfg.commission);'),
    ('(\\ as HTMLInputElement).value = String(cfg.reentry);', '($("bs-reentry") as HTMLInputElement).value = String(cfg.reentry);'),
    ('(\\ as HTMLInputElement).checked = cfg.overnight;', '($("bs-overnight") as HTMLInputElement).checked = cfg.overnight;'),
    ('(\\ as HTMLInputElement).value = String(cfg.confirmFlip);', '($("bs-confirm-flip") as HTMLInputElement).value = String(cfg.confirmFlip);'),
    ('(\\ as HTMLInputElement).value = String(cfg.ensembleQuorum);', '($("bs-quorum") as HTMLInputElement).value = String(cfg.ensembleQuorum);'),
    # save handler
    ('Number((\\ as HTMLInputElement).value) || 0.3);', 'Number(($("bs-commission") as HTMLInputElement).value) || 0.3);'),
    ('Number((\\ as HTMLInputElement).value) || 15);', 'Number(($("bs-reentry") as HTMLInputElement).value) || 15);'),
    ('(\\ as HTMLInputElement).checked;', '($("bs-overnight") as HTMLInputElement).checked;'),
    ('Number((\\ as HTMLInputElement).value) || 2);', 'Number(($("bs-confirm-flip") as HTMLInputElement).value) || 2);'),
    ('Number((\\ as HTMLInputElement).value) || 2);', 'Number(($("bs-quorum") as HTMLInputElement).value) || 2);'),
]

for old, new in fixes:
    if old in content:
        content = content.replace(old, new)
        print(f"Fixed: {old[:50]}...")
    else:
        print(f"NOT FOUND: {old[:50]}...")

# Check for remaining broken patterns
broken = re.findall(r'\\ as HTMLInputElement', content)
print(f"\nRemaining broken: {len(broken)}")

# Also fix "overlay.classList.remove" that might have lost its line
# Check the open() handler has all 5 new assignments
for field in ['bs-commission', 'bs-reentry', 'bs-overnight', 'bs-confirm-flip', 'bs-quorum']:
    if field in content:
        print(f"  {field}: OK")
    else:
        print(f"  {field}: MISSING!")

with open(path, 'w') as f:
    f.write(content)
print("\nDone")
