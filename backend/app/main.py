from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import mapping, records, schema, upload
from .services import database as db

@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    # A run left 'running' by a previous process can never finish; mark it failed.
    with db.conn() as c:
        c.execute("UPDATE mapping_runs SET status='failed', error_type='INTERRUPTED', error_message='Backend restarted while the run was in progress.' WHERE status='running'")
    yield


app = FastAPI(title="AI Schema Mapping Prototype", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"], allow_methods=["*"], allow_headers=["*"])



for r in (schema.router, upload.router, mapping.router, records.router):
    app.include_router(r)
