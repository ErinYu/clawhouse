#!/usr/bin/env python3
"""
MedDebate Web Backend - FastAPI SSE streaming server.
Run: python3 -m uvicorn backend:app --reload --port 8000
"""

import asyncio
import json
import os
import sys
from typing import Optional

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")

load_dotenv(os.path.join(_ROOT, ".env"))

sys.path.insert(0, _ROOT)
from scripts.demo_cases import DEMO_CASES
from scripts.specialists import SPECIALISTS

app = FastAPI(title="MedDebate API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = anthropic.AsyncAnthropic()

# In-memory session store: session_id -> {case, lang, phase1_results, user_diagnosis}
sessions: dict = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _sse(event_type: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **data})}\n\n"


def _lang_instruction(lang: str) -> str:
    if lang == "zh":
        return (
            "\n请用简体中文回复所有内容，包括诊断名称（括号内可附英文ICD名）、证据要点和推理过程。"
        )
    return ""


def _sse_headers() -> dict:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
    }


# ── Phase 1 agent ──────────────────────────────────────────────────────────────

async def _phase1_agent(specialist: dict, case: str, queue: asyncio.Queue, lang: str = "en") -> None:
    lang_note = _lang_instruction(lang)
    system = (
        f"You are {specialist['name']}, a specialist in {specialist['title']}.\n"
        f"Your focus: {specialist['focus']}.\n"
        f"Analyze the patient case from your specialty's perspective.{lang_note}\n"
        "Respond ONLY with valid JSON (no markdown fences, no preamble):\n"
        '{"diagnosis":"string","icd10":"string","confidence":75,'
        '"evidence":["p1","p2","p3"],"workup":["t1","t2"],"red_flags":["f1"]}'
    )
    try:
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": f"Patient case:\n{case}\n\nProvide your independent diagnosis as JSON."}],
        )
        result = json.loads(_strip_json(msg.content[0].text))
    except Exception as e:
        result = {
            "diagnosis": "Unable to assess" if lang == "en" else "无法评估",
            "icd10": "Z99.9",
            "confidence": 0,
            "evidence": [],
            "workup": [],
            "red_flags": [f"Agent error: {str(e)[:60]}"],
            "error": True,
        }
    result["specialist"] = specialist
    await queue.put({"type": "agent_result", "result": result})


# ── Phase 2 agent ──────────────────────────────────────────────────────────────

async def _phase2_agent(
    specialist: dict,
    case: str,
    phase1_results: list,
    queue: asyncio.Queue,
    lang: str = "en",
    user_diagnosis: Optional[str] = None,
) -> None:
    my_p1 = next(
        (r for r in phase1_results if r.get("specialist", {}).get("id") == specialist["id"]),
        None,
    )
    if not my_p1:
        await queue.put({"type": "debate_skip", "specialist": specialist})
        return

    others_summary = "\n".join(
        f"- {r['specialist']['name']} ({r['specialist']['title']}): "
        f"{r.get('diagnosis', 'unknown')} ({r.get('confidence', 50)}% confidence)"
        for r in phase1_results
        if r.get("specialist", {}).get("id") != specialist["id"]
    )

    is_house = specialist["id"] == "critic"
    house_note = (
        "\nYou are the senior diagnostician. Be blunt and provocative. "
        "You MUST challenge at least 2 colleagues with pointed, specific reasoning. "
        "Find the diagnosis everyone else missed or overlooked."
        if is_house else ""
    )

    user_note = ""
    if user_diagnosis:
        if lang == "zh":
            user_note = (
                f"\n\n【重要】玩家（一名医学生）也提交了他们的诊断：\"{user_diagnosis}\"。"
                "请明确回应玩家的诊断——赞同还是质疑？给出具体理由。"
            )
        else:
            user_note = (
                f"\n\nIMPORTANT: A player (medical student) has submitted their diagnosis: \"{user_diagnosis}\". "
                "Please specifically address the player's input — agree or challenge with concrete reasoning."
            )

    lang_note = _lang_instruction(lang)

    system = (
        f"You are {specialist['name']}, {specialist['title']}.{house_note}\n"
        f"You've seen your colleagues' Phase 1 diagnoses. Now debate.{lang_note}\n"
        "Respond ONLY with valid JSON (no markdown):\n"
        '{"final_diagnosis":"string","confidence":80,"changed_mind":false,'
        '"challenges":[{"doctor":"Dr. Name","reason":"specific reason"}],'
        '"agrees_with":["Dr. Name"],"reasoning":"1-2 sentences",'
        '"player_response":"optional response to player (empty string if no player)"}'
    )

    try:
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"Patient case: {case}\n\n"
                    f"Your Phase 1 diagnosis: {my_p1.get('diagnosis')} "
                    f"({my_p1.get('confidence', 50)}% confidence)\n\n"
                    f"Other specialists:\n{others_summary}"
                    f"{user_note}\n\n"
                    "Debate. Challenge or agree. Has your view changed?"
                ),
            }],
        )
        result = json.loads(_strip_json(msg.content[0].text))
    except Exception as e:
        result = {
            "final_diagnosis": my_p1.get("diagnosis", "Unknown"),
            "confidence": my_p1.get("confidence", 50),
            "changed_mind": False,
            "challenges": [],
            "agrees_with": [],
            "reasoning": f"Error: {str(e)[:60]}",
            "player_response": "",
        }

    result["specialist"] = specialist
    result["original_diagnosis"] = my_p1.get("diagnosis")
    await queue.put({"type": "debate", "result": result})


# ── Consensus ──────────────────────────────────────────────────────────────────

def _consensus(phase2_results: list) -> dict:
    vote_counts: dict = {}
    conf_sums: dict = {}
    specs_per: dict = {}
    for r in phase2_results:
        dx = r.get("final_diagnosis", "Unknown")
        conf = r.get("confidence", 50)
        vote_counts[dx] = vote_counts.get(dx, 0) + 1
        conf_sums[dx] = conf_sums.get(dx, 0.0) + conf
        specs_per.setdefault(dx, []).append(r["specialist"]["name"])
    ranked = sorted(
        [{"diagnosis": dx, "votes": v, "avg_confidence": round(conf_sums[dx] / v), "specialists": specs_per[dx]}
         for dx, v in vote_counts.items()],
        key=lambda x: (x["votes"], x["avg_confidence"]),
        reverse=True,
    )
    return {"ranked": ranked[:5]}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/api/cases/demo")
async def get_demo_cases():
    return {k: {"title": v["title"], "subtitle": v["subtitle"]} for k, v in DEMO_CASES.items()}


@app.get("/api/debate/phase1")
async def phase1_endpoint(
    session_id: str = Query(...),
    case: Optional[str] = Query(None),
    demo: Optional[str] = Query(None),
    lang: str = Query("en"),
):
    if demo and demo in DEMO_CASES:
        case_text = DEMO_CASES[demo]["description"]
    elif case:
        case_text = case
    else:
        return {"error": "Provide 'case' or 'demo'"}

    sessions[session_id] = {"case": case_text, "lang": lang, "phase1_results": [], "user_diagnosis": None}

    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        tasks = [asyncio.create_task(_phase1_agent(s, case_text, q, lang)) for s in SPECIALISTS]
        phase1_results = []
        for _ in range(len(SPECIALISTS)):
            item = await q.get()
            if item["type"] == "agent_result":
                phase1_results.append(item["result"])
                yield _sse("agent_result", {"phase": 1, "result": item["result"]})
        await asyncio.gather(*tasks, return_exceptions=True)
        sessions[session_id]["phase1_results"] = phase1_results
        yield _sse("phase1_complete", {"total": len(phase1_results)})
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/debate/user_input/{session_id}")
async def submit_user_input(session_id: str, user_diagnosis: str = Query(...)):
    if session_id not in sessions:
        return {"error": "Session not found"}
    sessions[session_id]["user_diagnosis"] = user_diagnosis
    return {"ok": True}


@app.get("/api/debate/phase2/{session_id}")
async def phase2_endpoint(session_id: str, lang: Optional[str] = Query(None)):
    if session_id not in sessions:
        return {"error": "Session not found"}
    session = sessions[session_id]
    case_text = session["case"]
    phase1_results = session["phase1_results"]
    user_diagnosis = session.get("user_diagnosis")
    if lang is None:
        lang = session.get("lang", "en")

    async def gen():
        if user_diagnosis:
            yield _sse("user_input_echo", {"diagnosis": user_diagnosis})
        q: asyncio.Queue = asyncio.Queue()
        tasks = [
            asyncio.create_task(_phase2_agent(s, case_text, phase1_results, q, lang, user_diagnosis))
            for s in SPECIALISTS
        ]
        phase2_results = []
        for _ in range(len(SPECIALISTS)):
            item = await q.get()
            if item["type"] == "debate":
                phase2_results.append(item["result"])
                yield _sse("debate", {"phase": 2, "result": item["result"]})
        await asyncio.gather(*tasks, return_exceptions=True)
        consensus = _consensus(phase2_results)
        yield _sse("consensus", {"data": consensus})
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_sse_headers())


@app.get("/")
async def root():
    with open(os.path.join(_HERE, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())
