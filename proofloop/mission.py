"""Per-idea validation missions. No autonomous outbound sending in this phase."""
import os
import json
import re
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS mission_plans(
 id BIGSERIAL PRIMARY KEY,
 idea_id BIGINT NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
 version INTEGER NOT NULL DEFAULT 1,
 engine TEXT NOT NULL,
 verdict TEXT NOT NULL DEFAULT 'needs_testing',
 summary TEXT NOT NULL DEFAULT '',
 strategy JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mission_actions(
 id BIGSERIAL PRIMARY KEY,
 plan_id BIGINT NOT NULL REFERENCES mission_plans(id) ON DELETE CASCADE,
 idea_id BIGINT NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL,
 title TEXT NOT NULL,
 type TEXT NOT NULL,
 channel TEXT NOT NULL DEFAULT 'web',
 objective TEXT NOT NULL DEFAULT '',
 query TEXT NOT NULL DEFAULT '',
 gate TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'proposed',
 findings JSONB NOT NULL DEFAULT '[]'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mission_drafts(
 id BIGSERIAL PRIMARY KEY,
 idea_id BIGINT NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
 lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
 subject TEXT NOT NULL,
 body TEXT NOT NULL,
 engine TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'unsent',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

def baseline_plan(idea):
    """Transparent, unverified outline until a real model is configured."""
    name=idea["title"]
    audience=idea["audience"] or "relevant users"
    description=(idea["description"] or "").lower()
    frame=any(term in (name+" "+description).lower() for term in ("frame","photos","samsung","television"))
    nursery=any(term in (name+" "+description).lower() for term in ("nurser","childcare","childminder"))
    coffee=any(term in (name+" "+description).lower() for term in ("roast","coffee","wholesale"))
    if frame:
        return {"verdict":"needs_testing","summary":"Consumer idea: prioritise existing requests, device/API feasibility and genuine willingness to pay before building. These are hypotheses, not research findings.",
        "audiences":["Frame TV owners wanting automatic photo updates","Home automation enthusiasts"],
        "assumptions":["The required TV integration is technically and legally feasible","Customers want recurring synchronisation enough to pay"],
        "actions":[
            {"title":"Investigate technical feasibility","type":"research","channel":"web","objective":"Confirm Samsung Frame TV photo integration limitations","query":"Samsung Frame TV Google Photos album sync SmartThings API photo automation","gate":"Cite official API constraints"},
            {"title":"Find existing requests","type":"research","channel":"web","objective":"Locate real, dated requests from owners","query":"Samsung Frame TV sync Google Photos album automatic","gate":"At least three distinct verifiable requests"},
            {"title":"Read relevant community rules","type":"community","channel":"forum","objective":"Find communities where direct and transparent product research is welcome","query":"Samsung Frame TV community photo sync discussions","gate":"Confirm platform permission before posting"},
            {"title":"Invite interested people into an opt-in pilot","type":"outreach","channel":"forum","objective":"Ask whether commenters still need it and would trial it","query":"","gate":"Explicit opted-in interest; no unsolicited DMs"},
            {"title":"Test paid intent","type":"validation","channel":"direct","objective":"Compare one-off versus subscription pricing with interested users","query":"","gate":"Credible willingness to pay, not just likes"}]}
    if nursery:
        return {"verdict":"needs_testing","summary":"Sensitive operational market: discover the most costly staff workflow and switching constraints before proposing a replacement. No child or parent records should be collected.",
        "audiences":["Independent nursery managers","Early years practitioners"],
        "assumptions":["Specific admin work is painful enough to fund a solution","Data protection and safeguarding can be handled"],
        "actions":[
            {"title":"Research recurring staff complaints","type":"research","channel":"web","objective":"Find reported inefficiencies, with sources","query":"nursery management software staff admin complaints Famly Tapestry","gate":"Corroborated pain points"},
            {"title":"Research software and compliance","type":"research","channel":"web","objective":"Find pricing, switching and data requirements","query":"UK nursery management app pricing migration security data","gate":"Clearly documented constraints"},
            {"title":"Find eligible operators","type":"prospecting","channel":"business","objective":"Identify relevant nursery decision-makers, respecting privacy","query":"independent UK nursery managers operational software","gate":"Verified business contacts and eligibility"},
            {"title":"Draft interview invitations","type":"outreach","channel":"email","objective":"Ask about their actual process without pitching a replacement","query":"","gate":"Approval and eligible recipient required"},
            {"title":"Validate a narrow pilot","type":"validation","channel":"direct","objective":"Find a paid test for one non-sensitive workflow","query":"","gate":"Actual commitment"}]}
    if coffee:
        return {"verdict":"needs_testing","summary":"B2B specialist idea: find Shopify roasters with a specific production or wholesale bottleneck, engage slowly and refine around real workflows.",
        "audiences":["Independent Shopify roasters","Roastery operations managers","Shopify agencies serving coffee businesses"],
        "assumptions":["Roast planning is costly enough to justify change","A Shopify-native solution fits existing operations"],
        "actions":[
            {"title":"Research roastery pain points","type":"research","channel":"web","objective":"Find repeated issues around wholesale, inventory and roast scheduling","query":"coffee roaster Shopify roast planning inventory spreadsheets wholesale production software","gate":"At least three sourced examples"},
            {"title":"Discover suitable roasters","type":"prospecting","channel":"business","objective":"Build a short list with website, tech stack evidence and genuine contact fit","query":"speciality coffee roasters Shopify wholesale UK","gate":"Research and qualify 6 initial businesses"},
            {"title":"Prepare six personal introductions","type":"outreach","channel":"email","objective":"Reference a verified public observation about each company and ask one honest question","query":"","gate":"No invented personalisation; verify marketing eligibility"},
            {"title":"Analyse replies and adapt","type":"validation","channel":"email","objective":"Determine the real bottleneck and demand, not just reply rate","query":"","gate":"Pause after first wave and revise"},
            {"title":"Qualify a paid pilot","type":"handover","channel":"direct","objective":"Confirm problem, buyer, timing and willingness to commit","query":"","gate":"Handover when real intent is established"}]}
    return {"verdict":"needs_testing","summary":"General-purpose exploratory strategy; needs source-backed market research before any contact. This is a rule-based outline, not an AI assessment.",
      "audiences":[audience],"assumptions":["The problem is repeated and important","Current alternatives do not adequately solve it"],
      "actions":[
        {"title":"Research customers' current workarounds","type":"research","channel":"web","objective":"Find verifiable pain signals","query":name+" alternatives problems feedback","gate":"Document independent sources"},
        {"title":"Analyse competitors and pricing","type":"research","channel":"web","objective":"Identify substitutes and market constraints","query":name+" software alternatives pricing","gate":"Evidence of unmet need"},
        {"title":"Find the right buyers","type":"prospecting","channel":"business","objective":"Locate eligible, relevant prospects","query":audience+" companies","gate":"Small qualified shortlist"},
        {"title":"Draft personalised discovery conversations","type":"outreach","channel":"email","objective":"Ask about workflow and need","query":"","gate":"Check recipient eligibility before contacting"},
        {"title":"Evaluate commercial commitment","type":"handover","channel":"direct","objective":"Identify a willing pilot customer","query":"","gate":"Explicit buying or pilot intent"}]}

SYSTEM = """You are a rigorous market-validation strategist for a solo technical founder.
Design a DIFFERENT evidence-based validation mission for each idea. The founder is skilled at coding
but dislikes sales and must avoid fixed costs, long builds, debt, hiring and large campaigns.
The goal is to discover real buyer problems and a qualified, willing person to hand over to the founder.
Evaluate validity as PRELIMINARY, not fact. Never fabricate companies, sources, quotes, results or replies.
Choose the right mix of research, prospect discovery, compliant B2B email, opt-in interviews,
community listening, permitted posting, landing pages, technical feasibility and partner discovery.
Propose sequential small-batch tests (3-6 initial email recipients max), A/B approaches when useful,
follow-up decision gates and explicit stop conditions.
No automated posting or messages without configured authorised channel and appropriate recipient eligibility.
Firecrawl may search/read websites but must NOT be represented as a general posting mechanism.
For Reddit assume only approved API, transparent app accounts and community rule compliance;
never scrape or send unsolicited DMs. No hidden marketing personas.
Output ONLY valid JSON with these keys:
verdict: string (needs_testing, promising_hypothesis, high_risk, poor_fit);
summary: string (max 350 chars);
audiences: array of strings (max 5);
assumptions: array of strings (max 5);
actions: array (4-8 objects) of
{title:string,type:string (research,prospecting,community,outreach,validation,handover),
channel:string,objective:string,query:string,gate:string}.
Plans are proposed tasks, not executed tasks.
"""

async def ask_ai_json(system, payload):
    key=os.getenv("OPENAI_API_KEY","")
    if not key: return None
    model=os.getenv("OPENAI_MODEL","gpt-6-luna")
    req={"model":model,"instructions":system,"input":json.dumps(payload),"max_output_tokens":1800,
         "text":{"format":{"type":"json_object"}}}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response=await client.post("https://api.openai.com/v1/responses",
                  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},
                  json=req)
        response.raise_for_status()
        data=response.json()
        chunks=[c.get("text","") for output in data.get("output",[]) for c in output.get("content",[]) if c.get("type")=="output_text"]
        if not chunks: raise ValueError("AI returned no text")
        return json.loads("\n".join(chunks))
    except Exception as exc:
        raise HTTPException(502,"AI planning failed. Verify the model, key and API account before retrying.") from exc

def normalise_plan(plan):
    if not isinstance(plan,dict) or not isinstance(plan.get("actions"),list) or not 3<=len(plan["actions"])<=10:
        raise HTTPException(502,"The model returned an invalid strategy. No plan was saved.")
    allowed_types={"research","prospecting","community","outreach","validation","handover"}
    actions=[]
    for a in plan["actions"]:
        if not isinstance(a,dict): continue
        typ=str(a.get("type","research"))
        if typ not in allowed_types: typ="research"
        actions.append({"title":str(a.get("title","Task"))[:200],"type":typ,
          "channel":str(a.get("channel","web"))[:80],"objective":str(a.get("objective",""))[:1000],
          "query":str(a.get("query",""))[:300],"gate":str(a.get("gate",""))[:1000]})
    if len(actions)<3: raise HTTPException(502,"Plan lacks actionable stages.")
    return {"verdict":str(plan.get("verdict","needs_testing"))[:60],
       "summary":str(plan.get("summary",""))[:1000],
       "audiences":[str(x)[:300] for x in plan.get("audiences",[])][:5],
       "assumptions":[str(x)[:400] for x in plan.get("assumptions",[])][:6],
       "actions":actions[:10]}

def mount(app, connect, ensure_auth, record):
    router=APIRouter()
    @app.on_event("startup")
    def mission_startup():
        with connect() as db:
            for stmt in SCHEMA.split(";"):
                if stmt.strip(): db.execute(stmt)

    @router.get("/missions/{idea_id}",response_class=HTMLResponse)
    def mission_page(idea_id:int):
        return Path(__file__).with_name("mission.html").read_text("utf-8")

    @router.get("/api/missions/{idea_id}")
    def get_mission(idea_id:int,request:Request):
        ensure_auth(request)
        with connect() as db:
            idea=db.execute("SELECT * FROM ideas WHERE id=%s",(idea_id,)).fetchone()
            if not idea: raise HTTPException(404,"Idea not found")
            plan=db.execute("SELECT * FROM mission_plans WHERE idea_id=%s ORDER BY version DESC LIMIT 1",(idea_id,)).fetchone()
            actions=db.execute("SELECT * FROM mission_actions WHERE plan_id=%s ORDER BY seq",(plan["id"],)).fetchall() if plan else []
            leads=db.execute("SELECT * FROM leads WHERE idea_id=%s ORDER BY id DESC",(idea_id,)).fetchall()
            drafts=db.execute("SELECT d.*,l.company,l.name,l.email FROM mission_drafts d JOIN leads l ON l.id=d.lead_id WHERE d.idea_id=%s ORDER BY d.id DESC LIMIT 100",(idea_id,)).fetchall()
        return {"idea":idea,"plan":plan,"actions":actions,"leads":leads,"drafts":drafts,
                "ai_configured":bool(os.getenv("OPENAI_API_KEY")),
                "firecrawl_configured":bool(os.getenv("FIRECRAWL_API_KEY")),
                "outbound_enabled":False}

    @router.post("/api/missions/{idea_id}/plan")
    async def generate_plan(idea_id:int,request:Request):
        ensure_auth(request)
        with connect() as db:
            idea=db.execute("SELECT * FROM ideas WHERE id=%s",(idea_id,)).fetchone()
            if not idea: raise HTTPException(404,"Idea not found")
            ev=db.execute("SELECT excerpt,source_url,strength FROM evidence WHERE idea_id=%s ORDER BY id DESC LIMIT 15",(idea_id,)).fetchall()
        real_ai=bool(os.getenv("OPENAI_API_KEY"))
        result=(await ask_ai_json(SYSTEM,{"idea":dict(idea),"recorded_evidence":ev,
                 "founder_constraints":"No fixed overhead, minimize sales, only qualified real leads"})) if real_ai else baseline_plan(idea)
        result=normalise_plan(result)
        with connect() as db:
            version=db.execute("SELECT coalesce(max(version),0)+1 AS v FROM mission_plans WHERE idea_id=%s",(idea_id,)).fetchone()["v"]
            plan=db.execute("INSERT INTO mission_plans(idea_id,version,engine,verdict,summary,strategy) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                (idea_id,version,"ai" if real_ai else "template",result["verdict"],result["summary"],Jsonb(result))).fetchone()
            for i,a in enumerate(result["actions"]):
                db.execute("""INSERT INTO mission_actions(plan_id,idea_id,seq,title,type,channel,objective,query,gate)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (plan["id"],idea_id,i+1,a["title"],a["type"],a["channel"],a["objective"],a["query"],a["gate"]))
            record(db,"mission_plan_created",f"{idea_id} v{version}, engine={'ai' if real_ai else 'template'}")
        return {"plan":plan,"engine":plan["engine"],"message":"AI strategy saved" if real_ai else "Rule-based preliminary outline saved. Connect OPENAI_API_KEY for real AI strategy."}

    class ActionUpdate(BaseModel):
        status: str
    @router.patch("/api/missions/actions/{action_id}")
    def set_action_status(action_id:int,data:ActionUpdate,request:Request):
        ensure_auth(request)
        if data.status not in ("proposed","queued","blocked","complete","skipped"):
            raise HTTPException(422,"Unknown status")
        with connect() as db:
            row=db.execute("UPDATE mission_actions SET status=%s,updated_at=now() WHERE id=%s RETURNING id",
                  (data.status,action_id)).fetchone()
            if not row: raise HTTPException(404,"Action not found")
            record(db,"mission_action_status",f"{action_id}: {data.status}")
        return {"ok":True}

    class DraftInput(BaseModel):
        lead_id:int
    @router.post("/api/missions/{idea_id}/draft")
    async def draft_intro(idea_id:int,data:DraftInput,request:Request):
        ensure_auth(request)
        with connect() as db:
            idea=db.execute("SELECT * FROM ideas WHERE id=%s",(idea_id,)).fetchone()
            lead=db.execute("SELECT * FROM leads WHERE id=%s AND idea_id=%s",(data.lead_id,idea_id)).fetchone()
        if not idea or not lead: raise HTTPException(404,"Idea or lead not found")
        if lead["eligibility"]=="suppressed": raise HTTPException(403,"This contact has opted out / is suppressed")
        if os.getenv("OPENAI_API_KEY"):
            system="""Write exactly one genuinely respectful and concise UK-English exploratory B2B email draft.
Based only on the supplied recorded facts. No invented details, praise, relationships or assumptions.
Ask about the prospect's CURRENT workflow and pain point; don't claim the product exists if it's an idea.
Identify the sender as a product researcher assisted by AI if AI is sending; this is a draft awaiting user approval.
Do not imply the recipient opted in. Include a clear sentence permitting no further contact.
Output JSON with subject (max 110 chars), body (max 800 chars), confidence_note."""
            draft=await ask_ai_json(system,{"idea":dict(idea),"lead":dict(lead),"purpose":"learn needs, not sell"})
            subject=str(draft.get("subject","Quick question about your workflow"))[:110]
            body=str(draft.get("body",""))[:2000]
            engine="ai"
        else:
            subject=f"Quick question about {idea['title']}"[:110]
            who=(lead["name"].split(" ")[0] if lead["name"] else "there")
            topic=(idea["description"] or idea["title"])[:160]
            notes=(lead["notes"] or "").strip()
            context=("\n\nI noticed: "+notes[:180]) if notes else ""
            body=f"Hi {who},\n\nI'm exploring whether {topic} would solve a real operational problem for businesses like {lead['company']}.{context}\n\nHow are you handling this today, and is there anything frustrating about the process?\n\nNo need to reply if this isn't relevant, and just let me know if you'd prefer no further contact.\n\nThanks,\nBob"
            engine="template"
        if not body.strip(): raise HTTPException(502,"No usable draft generated")
        with connect() as db:
            row=db.execute("INSERT INTO mission_drafts(idea_id,lead_id,subject,body,engine) VALUES(%s,%s,%s,%s,%s) RETURNING id",
              (idea_id,data.lead_id,subject,body,engine)).fetchone()
            record(db,"mission_draft_created",f"idea {idea_id}, lead {data.lead_id}, UNSENT")
        return {"id":row["id"],"subject":subject,"body":body,"engine":engine,"sent":False,
            "note":"Draft only. Sending is not implemented or authorised."}

    class SearchInput(BaseModel):
        action_id:int
    @router.post("/api/missions/{idea_id}/research")
    async def run_research(idea_id:int,data:SearchInput,request:Request):
        ensure_auth(request)
        with connect() as db:
            action=db.execute("SELECT * FROM mission_actions WHERE id=%s AND idea_id=%s",
                        (data.action_id,idea_id)).fetchone()
        if not action or action["type"] not in ("research","prospecting","community"):
            raise HTTPException(422,"Only research/discovery actions can be run")
        query=action["query"].strip() or action["title"]
        if not os.getenv("FIRECRAWL_API_KEY"):
            raise HTTPException(503,"Firecrawl research key not configured. No search was run.")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r=await client.post("https://api.firecrawl.dev/v2/search",
                    headers={"Authorization":"Bearer "+os.environ["FIRECRAWL_API_KEY"]},
                    json={"query":query,"limit":8})
                r.raise_for_status()
                payload=r.json()
            matches=payload.get("data",[])
            if isinstance(matches,dict): matches=matches.get("web",[])
            findings=[]
            for f in matches[:8]:
                url=str(f.get("url") or f.get("metadata",{}).get("sourceURL",""))
                if urlparse(url).scheme not in ("http","https"): continue
                findings.append({"title":str(f.get("title",""))[:240],
                    "description":str(f.get("description") or f.get("markdown",""))[:1500],
                    "url":url,"source":"Firecrawl","verified":False})
        except Exception as exc:
            raise HTTPException(502,"External research provider unavailable or not authorised.") from exc
        with connect() as db:
            db.execute("UPDATE mission_actions SET findings=%s,status='complete',updated_at=now() WHERE id=%s",
                (Jsonb(findings),data.action_id))
            record(db,"mission_research",f"action {data.action_id}: {len(findings)} source results")
        return {"query":query,"findings":findings,"count":len(findings),
            "note":"Search matches only. No claims of commercial validation. Site terms and permissions must be observed."}

    app.include_router(router)
