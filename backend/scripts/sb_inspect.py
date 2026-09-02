#!/usr/bin/env python3
"""Inspect t_tech sandbox order API signatures."""
import inspect

import t_tech.invest.grpc as grpc_mod
SandboxClient = getattr(grpc_mod, "SandboxClient")
from t_tech.invest.services import Services

print("=== SandboxClient order methods ===")
for name in dir(SandboxClient):
    if "order" in name.lower() and not name.startswith("_"):
        obj = getattr(SandboxClient, name)
        if callable(obj):
            try:
                print(f"  {name}{inspect.signature(obj)}")
            except (ValueError, TypeError):
                print(f"  {name} (no sig)")
    if name in ("orders", "sandbox"):
        obj = getattr(SandboxClient, name)
        print(f"  attr: {name} -> {obj}")

print()
print("=== Services order methods ===")
for name in dir(Services):
    if "order" in name.lower() and not name.startswith("_"):
        obj = getattr(Services, name)
        try:
            print(f"  {name}{inspect.signature(obj)}")
        except (ValueError, TypeError):
            print(f"  {name} (no sig)")
