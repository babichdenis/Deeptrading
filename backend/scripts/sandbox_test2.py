import sys
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")

LIVE_TOKEN = "t.LwZSK4Lj4HfS4ILhm-wIw-DPbGm6cEN0Ixv8tmm0oTlfzDIaI_RhAg-gsJu5zuqPUBmTuQoh1eTt8Y56I_Gknw"

try:
    from t_tech.invest import Client
    with Client(LIVE_TOKEN) as c:
        accs = c.users.get_accounts()
        for a in accs.accounts:
            print(f"Account: {a.id} name={a.name} status={a.status}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
