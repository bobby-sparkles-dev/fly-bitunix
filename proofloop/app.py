import os
import re
import secrets
import hmac
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not DATABASE_URL or not DASHBOARD_PASSWORD or not SESSION_SECRET:
    raise RuntimeError("DATABASE_URL, DASHBOARD_PASSWORD and SESSION_SECRET must be configured")

app = FastAPI(title="Proofloop", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="strict",
    https_only=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
    max_age=60 * 60 * 24 * 7,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
  id BIGSERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  audience TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('user','discovery')),
  stage TEXT NOT NULL DEFAULT 'hypothesis',
  decision TEXT NOT NULL DEFAULT 'research',
  notes TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS evidence (
  id BIGSERIAL PRIMARY KEY,
  idea_id BIGINT NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'observation',
  source_name TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL,
  strength TEXT NOT NULL DEFAULT 'weak',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS leads (
  id BIGSERIAL PRIMARY KEY,
  idea_id BIGINT REFERENCES ideas(id) ON DELETE SET NULL,
  company TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  website TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'uncontacted',
  eligibility TEXT NOT NULL DEFAULT 'unverified',
  notes TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS experiments (
  id BIGSERIAL PRIMARY KEY,
  idea_id BIGINT NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  hypothesis TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT 'email',
  variant_a TEXT NOT NULL DEFAULT '',
  variant_b TEXT NOT NULL DEFAULT '',
  wave_size INTEGER NOT NULL DEFAULT 6 CHECK (wave_size BETWEEN 1 AND 20),
  contacted INTEGER NOT NULL DEFAULT 0,
  replied INTEGER NOT NULL DEFAULT 0,
  engaged INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'draft',
  notes TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS conversations (
  id BIGSERIAL PRIMARY KEY,
  idea_id BIGINT REFERENCES ideas(id) ON DELETE SET NULL,
  lead_id BIGINT REFERENCES leads(id) ON DELETE SET NULL,
  channel TEXT NOT NULL DEFAULT 'email',
  direction TEXT NOT NULL DEFAULT 'inbound',
  body TEXT NOT NULL,
  intent TEXT NOT NULL DEFAULT 'unclassified',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research_runs (
  id BIGSERIAL PRIMARY KEY,
  idea_id BIGINT REFERENCES ideas(id) ON DELETE SET NULL,
  query TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'complete',
  result_count INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'targeted',
  findings JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_events (
  id BIGSERIAL PRIMARY KEY,
  event TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO settings(key,value) VALUES
 ('outbound_enabled','false'),('max_new_contacts_week','20'),
 ('max_active_ideas','2'),('research_mode','manual')
 ON CONFLICT(key) DO NOTHING;
"""
def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)

def setup():
    with connect() as db:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        count = db.execute("SELECT count(*) AS n FROM ideas").fetchone()["n"]
        if count == 0:
            for title, desc, audience in [
                ("Roastery.ops", "Shopify-native roast planning, production inventory and wholesale operations, refined with real roasteries.", "Independent Shopify coffee roasters"),
                ("Google Photos to Samsung Frame", "Sync a chosen Google Photos album to Samsung Frame TVs with minimal manual intervention.", "Samsung Frame TV owners"),
                ("Nursery administration", "Lower-screen-time workflows for staff and clear parent communication and funding administration.", "Independent nursery operators"),
            ]:
                db.execute("INSERT INTO ideas(title,description,audience) VALUES (%s,%s,%s)", (title,desc,audience))
            db.execute("INSERT INTO audit_events(event,detail) VALUES ('seed','Created three initial hypotheses')")
@app.on_event("startup")
def on_startup():
    setup()

def ensure_auth(request: Request):
    if request.session.get("owner") is not True:
        raise HTTPException(401, "Sign in to access Proofloop")

def record(db, event, detail=""):
    db.execute("INSERT INTO audit_events(event,detail) VALUES (%s,%s)", (event, detail[:500]))

@app.get("/health")
def health():
    try:
        with connect() as db:
            db.execute("SELECT 1")
        return {"ok": True, "storage": "postgres", "outbound": "disabled"}
    except Exception:
        return JSONResponse({"ok": False, "storage": "unavailable"}, status_code=503)

@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).with_name("index.html").read_text("utf-8")

class Login(BaseModel):
    password: str

@app.post("/api/login")
def login(data: Login, request: Request):
    if not hmac.compare_digest(data.password.encode(), DASHBOARD_PASSWORD.encode()):
        raise HTTPException(401, "Incorrect password")
    request.session["owner"] = True
    return {"ok": True}

@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/api/me")
def me(request: Request):
    return {"authenticated": request.session.get("owner") is True}

@app.get("/api/dashboard")
def dashboard(request: Request):
    ensure_auth(request)
    with connect() as db:
        ideas = db.execute("""SELECT i.*, (SELECT count(*) FROM evidence e WHERE e.idea_id=i.id) AS evidence_count,
         (SELECT count(*) FROM leads l WHERE l.idea_id=i.id) AS lead_count,
         (SELECT count(*) FROM experiments x WHERE x.idea_id=i.id) AS experiment_count
         FROM ideas i ORDER BY i.updated_at DESC, i.id DESC""").fetchall()
        leads = db.execute("SELECT l.*,i.title AS idea_title FROM leads l LEFT JOIN ideas i ON i.id=l.idea_id ORDER BY l.id DESC LIMIT 100").fetchall()
        experiments = db.execute("SELECT e.*,i.title AS idea_title FROM experiments e JOIN ideas i ON i.id=e.idea_id ORDER BY e.id DESC LIMIT 100").fetchall()
        evidence = db.execute("SELECT e.*,i.title AS idea_title FROM evidence e JOIN ideas i ON i.id=e.idea_id ORDER BY e.id DESC LIMIT 80").fetchall()
        research = db.execute("SELECT r.id,r.query,r.mode,r.result_count,r.created_at,i.title AS idea_title,r.findings FROM research_runs r LEFT JOIN ideas i ON i.id=r.idea_id ORDER BY r.id DESC LIMIT 30").fetchall()
        conversations = db.execute("SELECT c.*,l.company,i.title AS idea_title FROM conversations c LEFT JOIN leads l ON l.id=c.lead_id LEFT JOIN ideas i ON i.id=c.idea_id ORDER BY c.id DESC LIMIT 100").fetchall()
        audit = db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 30").fetchall()
        settings = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings").fetchall()}
    return {"ideas":ideas,"leads":leads,"experiments":experiments,"evidence":evidence,
            "research":research,"conversations":conversations,"events":audit,"settings":settings}

class IdeaIn(BaseModel):
    title: str = Field(min_length=3,max_length=160)
    description: str = Field(default="",max_length=4000)
    audience: str = Field(default="",max_length=500)
    origin: str = "user"

@app.post("/api/ideas")
def create_idea(data: IdeaIn, request: Request):
    ensure_auth(request)
    if data.origin not in ("user","discovery"): raise HTTPException(422,"Invalid origin")
    with connect() as db:
        row = db.execute("INSERT INTO ideas(title,description,audience,origin) VALUES (%s,%s,%s,%s) RETURNING *",
                 (data.title,data.description,data.audience,data.origin)).fetchone()
        record(db,"idea_created",data.title)
    return row

class IdeaUpdate(BaseModel):
    stage: Optional[str] = None
    decision: Optional[str] = None
    notes: Optional[str] = None
    description: Optional[str] = None

@app.patch("/api/ideas/{idea_id}")
def update_idea(idea_id: int, data: IdeaUpdate, request: Request):
    ensure_auth(request)
    allowed_stages = ("hypothesis","corroborated","demand","commercial","build")
    allowed_decisions = ("research","test","build","refine","park")
    updates = data.model_dump(exclude_unset=True)
    if "stage" in updates and updates["stage"] not in allowed_stages: raise HTTPException(422,"Invalid stage")
    if "decision" in updates and updates["decision"] not in allowed_decisions: raise HTTPException(422,"Invalid decision")
    with connect() as db:
        row = db.execute("SELECT id FROM ideas WHERE id=%s",(idea_id,)).fetchone()
        if not row: raise HTTPException(404,"Idea not found")
        for key,value in updates.items():
            if value is not None: db.execute("UPDATE ideas SET "+key+"=%s, updated_at=now() WHERE id=%s",(value,idea_id))
        record(db,"idea_updated",str(idea_id))
    return {"ok":True}

class EvidenceIn(BaseModel):
    idea_id: int
    excerpt: str = Field(min_length=3,max_length=5000)
    kind: str = "observation"
    source_name: str = ""
    source_url: str = ""
    strength: str = "weak"

@app.post("/api/evidence")
def create_evidence(data: EvidenceIn, request: Request):
    ensure_auth(request)
    with connect() as db:
        row = db.execute("INSERT INTO evidence(idea_id,excerpt,kind,source_name,source_url,strength) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
            (data.idea_id,data.excerpt,data.kind,data.source_name,data.source_url,data.strength)).fetchone()
        record(db,"evidence_added",str(data.idea_id))
    return row

class LeadIn(BaseModel):
    idea_id: Optional[int] = None
    company: str = Field(min_length=1,max_length=250)
    name: str = ""
    email: str = ""
    website: str = ""
    source: str = ""
    eligibility: str = "unverified"
    notes: str = ""

@app.post("/api/leads")
def create_lead(data: LeadIn, request: Request):
    ensure_auth(request)
    with connect() as db:
        row = db.execute("INSERT INTO leads(idea_id,company,name,email,website,source,eligibility,notes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (data.idea_id,data.company,data.name,data.email,data.website,data.source,data.eligibility,data.notes)).fetchone()
        record(db,"lead_added",data.company)
    return row

class ExperimentIn(BaseModel):
    idea_id: int
    title: str = Field(min_length=3,max_length=200)
    hypothesis: str = ""
    channel: str = "email"
    variant_a: str = ""
    variant_b: str = ""
    wave_size: int = Field(default=6,ge=1,le=20)

@app.post("/api/experiments")
def create_experiment(data: ExperimentIn,request: Request):
    ensure_auth(request)
    with connect() as db:
        row = db.execute("INSERT INTO experiments(idea_id,title,hypothesis,channel,variant_a,variant_b,wave_size) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
          (data.idea_id,data.title,data.hypothesis,data.channel,data.variant_a,data.variant_b,data.wave_size)).fetchone()
        record(db,"experiment_drafted",data.title)
    return row

class ConversationIn(BaseModel):
    idea_id: Optional[int] = None
    lead_id: Optional[int] = None
    channel: str = "email"
    direction: str = "inbound"
    intent: str = "unclassified"
    body: str = Field(min_length=1,max_length=20000)

@app.post("/api/conversations")
def create_conversation(data: ConversationIn,request: Request):
    ensure_auth(request)
    with connect() as db:
        row = db.execute("INSERT INTO conversations(idea_id,lead_id,channel,direction,intent,body) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
           (data.idea_id,data.lead_id,data.channel,data.direction,data.intent,data.body)).fetchone()
        record(db,"conversation_logged",data.intent)
    return row

class ResearchIn(BaseModel):
    idea_id: Optional[int] = None
    query: str = Field(default="",max_length=160)
    mode: str = "targeted"

def clean(text):
    return re.sub(r"<[^>]+>", " ", str(text or "")).replace("&quot;",'"').replace("&#x27;","'").strip()[:900]

@app.post("/api/research/run")
async def run_research(data: ResearchIn, request: Request):
    ensure_auth(request)
    if data.mode not in ("targeted","discovery"): raise HTTPException(422,"Invalid research mode")
    query = data.query.strip()
    if data.mode == "targeted" and not query:
        if not data.idea_id: raise HTTPException(422,"Choose an idea or supply a query")
        with connect() as db:
            idea = db.execute("SELECT title,audience FROM ideas WHERE id=%s",(data.idea_id,)).fetchone()
            if not idea: raise HTTPException(404,"Idea not found")
            query = (idea["title"]+" "+idea["audience"])[:160]
    if data.mode == "discovery" and not query:
        query = "manual spreadsheet workflows"
    url = "https://hn.algolia.com/api/v1/search_by_date"
    try:
        async with httpx.AsyncClient(timeout=12,follow_redirects=True) as client:
            res = await client.get(url,params={"query":query,"tags":"story","hitsPerPage":15})
            res.raise_for_status()
            hits = res.json().get("hits",[])
    except Exception:
        raise HTTPException(502,"Research provider temporarily unavailable. No findings were stored.")
    findings = []
    for hit in hits[:15]:
        title = clean(hit.get("title") or hit.get("story_title"))
        if not title: continue
        findings.append({
            "title":title,
            "excerpt":clean(hit.get("story_text")),
            "url":hit.get("url") or ("https://news.ycombinator.com/item?id="+str(hit.get("objectID",""))),
            "source":"Hacker News", "date":hit.get("created_at",""),
            "object_id":str(hit.get("objectID",""))
        })
    from psycopg.types.json import Jsonb
    with connect() as db:
        db.execute("INSERT INTO research_runs(idea_id,query,mode,result_count,findings) VALUES (%s,%s,%s,%s,%s)",
             (data.idea_id,query,data.mode,len(findings),Jsonb(findings)))
        record(db,"research_completed",query)
    return {"query":query,"mode":data.mode,"findings":findings,"count":len(findings),
            "note":"Source matches are research leads, not proof of buyer demand. Review sources before creating evidence or contacting anyone."}

@app.get("/api/research/status")
def research_status(request: Request):
    ensure_auth(request)
    return {"provider":"Hacker News Algolia","mode":"on demand","automatic_outreach":False,"forum_posting":False,
            "email_sending":False,"openai_agent_connected":False}

@app.exception_handler(psycopg.Error)
async def database_error(_request: Request, _exc: psycopg.Error):
    return JSONResponse({"detail":"Database operation failed. Check the supplied fields and retry."}, status_code=400)


# Adaptive, per-idea validation missions
from . import mission
mission.mount(app, connect, ensure_auth, record)
