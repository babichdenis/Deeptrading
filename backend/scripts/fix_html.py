path = "/Users/Denis/Dev/Deeptrading/frontend/index.html"
with open(path) as f:
    c = f.read()

old = '''        <div class="bs-grid" style="margin-top:8px">
          <label class="lg-chip" title="\u0417\u0430\u043a\u044b\u0432\u0430\u0442\u044c \u0432\u0441\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438 \u0432 \u043a\u043e\u043d\u0446\u0435 \u0442\u043e\u0440\u0433\u043e\u0432\u043e\u0439 \u0441\u0435\u0441\u0441\u0438\u0438"><input type="checkbox" id="bs-overnight"><span>Overnight (\u0437\u0430\u043a\u0440\u044b\u0442\u0438\u0435 EOD)</span></label>
        </div>'''

new = '''        <div class="bs-grid" style="margin-top:8px">
          <label class="lg-chip" title="\u0417\u0430\u043a\u044b\u0432\u0430\u0442\u044c \u0432\u0441\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438 \u0432 \u043a\u043e\u043d\u0446\u0435 \u0442\u043e\u0440\u0433\u043e\u0432\u043e\u0439 \u0441\u0435\u0441\u0441\u0438\u0438"><input type="checkbox" id="bs-overnight"><span>Overnight (\u0437\u0430\u043a\u0440\u044b\u0442\u0438\u0435 EOD)</span></label>
        </div>
        <div class="bs-grid" style="margin-top:8px">
          <label>Confirm flip (\u0432\u0441\u0442\u0440\u0435\u0447\u043d\u044b\u0445 \u043f\u0435\u0440\u0435\u0434 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u0435\u043c)<input type="number" id="bs-confirm-flip" value="2" step="1" min="0" max="10" /></label>
          <label>Quorum (\u0433\u043e\u043b\u043e\u0441\u043e\u0432 \u0434\u043b\u044f \u0432\u0445\u043e\u0434\u0430)<input type="number" id="bs-quorum" value="2" step="1" min="1" max="7" /></label>
        </div>'''

if old in c:
    c = c.replace(old, new, 1)
    print("HTML updated")
else:
    print("OLD pattern not found!")
    # Try to find approximate
    if "bs-overnight" in c:
        print("bs-overnight found in HTML")
    else:
        print("bs-overnight NOT found")

with open(path, 'w') as f:
    f.write(c)
