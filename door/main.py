"""FastAPI public door and the locally served demo console."""
from __future__ import annotations

import os
import asyncio
import time
from contextlib import suppress
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from door.service import DoorService


def create_app(data_dir: str | Path | None = None, fixture: bool = False, *, idle_clock=None) -> FastAPI:
    load_dotenv()
    folder = Path(data_dir or os.getenv("DATA_DIR", "data"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        from door.catalog import Catalog
        from door.matching import Matcher
        from door.valio import ValioClient
        from door.decisions import DecisionLabeler

        folder.mkdir(parents=True, exist_ok=True)
        catalog = Catalog(folder / "catalog.sqlite3")
        if catalog.count() == 0 or catalog.needs_rebuild():
            if fixture:
                catalog.seed_fixture()
            else:
                source = Path(__file__).resolve().parents[2] / "valio_aimo_junction26_product_data.json"
                if not source.exists():
                    source = folder / "valio_aimo_junction26_product_data.json"
                if not source.exists():
                    from scripts.fetch_data import fetch
                    await fetch(folder)
                catalog.build(source, source.parent / "valio_aimo_junction26_delivery_reliability.csv", source.parent / "valio_aimo_junction26_carbon_footprint.csv")
        if fixture:
            # Extra named search examples belong to the app fixture. The original
            # Catalog.seed_fixture() unit fixture remains byte-for-byte compatible.
            catalog.seed_fixture(conversation=True)
        valio = ValioClient(folder, fixture=fixture)
        application.state.service = DoorService(folder, catalog, valio, Matcher(catalog, valio, DecisionLabeler(folder, enabled=not fixture)), fixture)
        await application.state.service.initialize_history()
        warm_task = None
        if not fixture:
            from door.bot_runtime import BotRuntime
            runtime = BotRuntime(folder)
            application.state.bot_runtime = runtime
            application.state.bot_provider = runtime.provider
            if os.getenv("AGENT_WARMUP_ENABLED", "1").lower() not in {"0", "false", "no"}:
                warm_task = asyncio.create_task(runtime.run())
        try:
            yield
        finally:
            if warm_task:
                warm_task.cancel()
                with suppress(asyncio.CancelledError):
                    await warm_task
            close = getattr(valio, "aclose", None)
            if close:
                await close()

    app = FastAPI(title="Aimo's Door", version="0.1.0", description="A public purchasing API enforcing buyer-signed limits. Order book, reservations and buyer keys are simulated.", lifespan=lifespan)
    from door.protection import DemoProtection
    from door.idle_reset import DemoIdleResetMiddleware
    app.add_middleware(DemoProtection)
    app.add_middleware(DemoIdleResetMiddleware, clock=idle_clock or time.monotonic)

    from door.model_budget import ModelLimitError
    from door.valio import UpstreamError, UsageLimitError

    @app.exception_handler(ModelLimitError)
    @app.exception_handler(UsageLimitError)
    async def usage_limit(request, error):
        return JSONResponse(status_code=429, content={"detail": {"message": str(error), "blocked": True, "reason": "usage_or_credit_limit"}})

    @app.exception_handler(UpstreamError)
    async def upstream_failure(request, error):
        return JSONResponse(status_code=503, content={"detail": str(error)})

    from door.bot_routes import router as bot_router
    app.include_router(bot_router)

    def service() -> DoorService:
        return app.state.service

    @app.get("/healthz", tags=["Foundation"])
    def health():
        import psutil
        return {"status": "ok", "products": service().catalog.count(), "fixture": service().fixture,
                "memory_rss_bytes": psutil.Process().memory_info().rss, "pid": os.getpid(),
                "restaurant_history": getattr(service(), "history_status", {"state": "unknown", "seeded": False}),
                "model_warmup": getattr(getattr(app.state, "bot_runtime", None), "status", {"state": "disabled"})}

    @app.get("/.well-known/jwks.json", tags=["Trust"])
    def jwks():
        return service().keys.jwks()

    @app.get("/demo/buyer-jwks", tags=["Demo"])
    def buyer_jwks():
        return {**service().keys.jwks(buyer=True), "simulated": True}

    @app.get("/catalog/search", tags=["Catalog"])
    async def catalog_search(text: str = "", category: str | None = None, properties: list[str] | None = Query(None), storage_class: str | None = None, limit: int = Query(50, ge=1, le=100), customer_id: str | None = None, delivery_date: str | None = None):
        return await service().catalog_search(text=text, category=category, properties=properties, storage_class=storage_class, limit=limit, customer_id=customer_id, delivery_date=delivery_date)

    @app.post("/mandates", tags=["Trust"])
    def register_mandate(body: dict = Body(...), x_buyer_session: str | None = Header(None)):
        return service().register_mandate(body.get("mandate", ""), x_buyer_session)

    @app.get("/mandates/{mandate_id}", tags=["Trust"])
    def get_mandate(mandate_id: str):
        with service().store.connect() as db:
            document = service().store.get(db, "mandate", mandate_id)
        if not document:
            raise HTTPException(404, "Mandate not found.")
        return {"payload": document["payload"]}

    @app.post("/needs/match", tags=["Purchasing"])
    async def match(body: dict = Body(...)):
        return await service().match(body)

    @app.post("/offers", tags=["Purchasing"])
    def offer(body: dict = Body(...)):
        return service().offer(body)

    @app.post("/orders", tags=["Purchasing"])
    def order(body: dict = Body(...), idempotency_key: str | None = Header(None)):
        return service().order(body, idempotency_key)

    @app.get("/orders/{order_id}", tags=["Purchasing"])
    async def get_order(order_id: str, x_buyer_session: str | None = Header(None), authorization: str | None = Header(None), x_bot_id: str | None = Header(None)):
        return await service().get_order(order_id, buyer_token=x_buyer_session, authorization=authorization, bot_id=x_bot_id)

    @app.post("/orders/{order_id}/signoff", tags=["Buyer actions"])
    async def signoff(order_id: str, body: dict = Body(...), x_buyer_session: str | None = Header(None)):
        return await service().signoff(order_id, body, x_buyer_session)

    @app.post("/orders/{order_id}/undo", tags=["Buyer actions"])
    def undo(order_id: str, x_buyer_session: str | None = Header(None)):
        return service().undo(order_id, x_buyer_session)

    @app.get("/records/{record_id}", tags=["Trust"])
    def get_record(record_id: str):
        return service().record(record_id)

    @app.get("/usual-order", tags=["Buyer actions"])
    async def usual(customer_id: str = "restaurant", x_buyer_session: str | None = Header(None), authorization: str | None = Header(None), x_bot_id: str | None = Header(None)):
        service().authorize_read(customer_id, buyer_token=x_buyer_session, authorization=authorization, bot_id=x_bot_id)
        await service().ensure_history(customer_id)
        return service().usual(customer_id)

    @app.get("/limits", tags=["Buyer actions"])
    async def limits(customer_id: str = "hotel", x_buyer_session: str | None = Header(None), authorization: str | None = Header(None), x_bot_id: str | None = Header(None)):
        service().authorize_read(customer_id, buyer_token=x_buyer_session, authorization=authorization, bot_id=x_bot_id)
        await service().ensure_history(customer_id)
        return service().limits(customer_id)

    @app.post("/limits/proposals/{proposal_id}/accept", tags=["Buyer actions"])
    def proposal(proposal_id: str, body: dict = Body(default={}), x_buyer_session: str | None = Header(None)):
        return service().accept_proposal(proposal_id, body, x_buyer_session)

    @app.post("/limits/{limit_id}/change", tags=["Buyer actions"])
    def change_limit(limit_id: str, body: dict = Body(...), x_buyer_session: str | None = Header(None), x_identification_proof: str | None = Header(None)):
        return service().change_limit(limit_id, body, x_buyer_session, x_identification_proof)

    @app.post("/limits/{limit_id}/reset", tags=["Buyer actions"])
    def reset_limit(limit_id: str, body: dict = Body(default={}), x_buyer_session: str | None = Header(None)):
        return service().reset_limit(limit_id, body, x_buyer_session)

    @app.get("/demo/customers", tags=["Demo"])
    def customers():
        return service().customers()

    @app.post("/demo/identify", tags=["Demo"])
    def identify(body: dict = Body(...), x_buyer_session: str | None = Header(None)):
        return service().identify(str(body.get("customer_id", "")), str(body.get("method", "")), x_buyer_session)

    @app.post("/demo/buyer-session", tags=["Demo"])
    def buyer_session(body: dict = Body(...)):
        return service().create_buyer_session(str(body.get("customer_id", "")))

    @app.post("/demo/reset", tags=["Demo"])
    def reset(body: dict = Body(default={}), x_buyer_session: str | None = Header(None)):
        # This simulated, whole-demo reset accepts either buyer. If a customer is
        # named explicitly, the credential must belong to that same customer.
        service().buyer_session(x_buyer_session, body.get("customer_id", "hotel"))
        return service().reset()

    frontend = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    assets = frontend / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def console():
        if (frontend / "index.html").exists():
            return FileResponse(frontend / "index.html")
        return HTMLResponse("<!doctype html><html><body><h1>Aimo's Door</h1><p>Build the console with npm --prefix frontend run build.</p><a href='/docs'>Public API documentation</a></body></html>", status_code=503)

    return app


app = create_app()
