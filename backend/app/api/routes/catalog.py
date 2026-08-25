from fastapi import APIRouter

from app.engine.catalog import STRATEGY_CATALOG
from app.services.experiments import EXIT_POLICY_SPECS

router = APIRouter(prefix="/api/v1/strategies", tags=["catalog"])


@router.get("/catalog")
async def get_catalog() -> dict:
    cards = [c.to_dict() for c in STRATEGY_CATALOG.values()]
    cards.sort(key=lambda c: (c["wave"], c["family"], c["id"]))
    exits = [
        {"id": pid, "label": spec["label"], "params_schema": spec["schema"]}
        for pid, spec in EXIT_POLICY_SPECS.items()
    ]
    return {
        "count": len(cards),
        "families": ["reversal", "pullback", "breakout", "momentum", "structure"],
        "strategies": cards,
        "exit_policies": exits,
    }
