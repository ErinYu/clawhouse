#!/usr/bin/env python3
"""
MedDebate CLI - 20 AI medical specialists debate a patient case.
Used by OpenClaw skill via exec tool, and by web/backend.py.

Usage:
    python debate_engine.py --case "Patient: 34F, fatigue, joint pain..."
    python debate_engine.py --demo lupus
"""

import asyncio
import json
from typing import Optional
import argparse
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from specialists import SPECIALISTS
from demo_cases import DEMO_CASES

client = anthropic.AsyncAnthropic()


def _strip_json(text: str) -> str:
    """Strip markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


async def reason_phase1(specialist: dict, case: str, queue: Optional[asyncio.Queue] = None) -> dict:
    """Single specialist reasons independently about the case."""
    system = (
        f"You are {specialist['name']}, a specialist in {specialist['title']}.\n"
        f"Your focus: {specialist['focus']}.\n"
        "A patient case will be presented. Analyze it purely from your specialty's perspective.\n"
        "Respond ONLY with valid JSON (no markdown, no extra text) with exactly these keys:\n"
        '{"diagnosis": "string", "icd10": "string", "confidence": 75, '
        '"evidence": ["point1", "point2", "point3"], '
        '"workup": ["test1", "test2"], "red_flags": ["flag1"]}'
    )

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": f"Patient case:\n{case}\n\nProvide your independent diagnosis as JSON.",
                }
            ],
        )
        result = json.loads(_strip_json(message.content[0].text))
        result["specialist"] = specialist
        result["phase"] = 1
    except Exception as e:
        result = {
            "specialist": specialist,
            "phase": 1,
            "diagnosis": "Unable to assess",
            "icd10": "Z99.9",
            "confidence": 0,
            "evidence": [],
            "workup": [],
            "red_flags": [f"Error: {str(e)[:80]}"],
            "error": True,
        }

    if queue:
        await queue.put({"type": "agent_result", "data": result})
    return result


async def debate_phase2(
    specialist: dict,
    case: str,
    phase1_results: list,
    queue: Optional[asyncio.Queue] = None,
) -> dict:
    """Specialist debates after seeing all Phase 1 results."""
    my_p1 = next(
        (r for r in phase1_results if r.get("specialist", {}).get("id") == specialist["id"]),
        None,
    )
    if not my_p1:
        return {}

    others = [r for r in phase1_results if r.get("specialist", {}).get("id") != specialist["id"]]
    others_summary = "\n".join(
        f"- {r['specialist']['name']} ({r['specialist']['title']}): "
        f"{r.get('diagnosis', 'unknown')} ({r.get('confidence', 50)}% confidence)"
        for r in others
    )

    is_house = specialist["id"] == "critic"
    house_note = (
        "\nYou are the senior diagnostician. Be blunt and contrarian. "
        "You MUST challenge at least 2 colleagues with specific, pointed reasoning. "
        "Find what everyone else missed."
        if is_house
        else ""
    )

    system = (
        f"You are {specialist['name']}, {specialist['title']}.{house_note}\n"
        "You have seen the initial diagnoses of your colleagues. Now debate.\n"
        "Respond ONLY with valid JSON:\n"
        '{"final_diagnosis": "string", "confidence": 80, "changed_mind": false, '
        '"challenges": [{"doctor": "Dr. Name", "reason": "specific reason"}], '
        '"agrees_with": ["Dr. Name"], "reasoning": "1-2 sentences"}'
    )

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Patient case: {case}\n\n"
                        f"Your Phase 1 diagnosis: {my_p1.get('diagnosis')} "
                        f"({my_p1.get('confidence', 50)}% confidence)\n\n"
                        f"Other specialists' diagnoses:\n{others_summary}\n\n"
                        "Now debate. Challenge or agree. Has your view changed?"
                    ),
                }
            ],
        )
        result = json.loads(_strip_json(message.content[0].text))
        result["specialist"] = specialist
        result["phase"] = 2
        result["original_diagnosis"] = my_p1.get("diagnosis")
    except Exception as e:
        result = {
            "specialist": specialist,
            "phase": 2,
            "final_diagnosis": my_p1.get("diagnosis", "Unknown"),
            "confidence": my_p1.get("confidence", 50),
            "changed_mind": False,
            "challenges": [],
            "agrees_with": [],
            "reasoning": f"Error in debate phase: {str(e)[:80]}",
            "original_diagnosis": my_p1.get("diagnosis"),
        }

    if queue:
        await queue.put({"type": "debate", "data": result})
    return result


def compute_consensus(phase2_results: list) -> dict:
    """Aggregate votes into ranked differential diagnosis."""
    vote_counts: dict[str, int] = {}
    confidence_sums: dict[str, float] = {}
    specialists_per_dx: dict[str, list] = {}

    for r in phase2_results:
        dx = r.get("final_diagnosis", "Unknown")
        conf = r.get("confidence", 50)
        vote_counts[dx] = vote_counts.get(dx, 0) + 1
        confidence_sums[dx] = confidence_sums.get(dx, 0.0) + conf
        specialists_per_dx.setdefault(dx, []).append(r["specialist"]["name"])

    ranked = sorted(
        [
            {
                "diagnosis": dx,
                "votes": v,
                "avg_confidence": round(confidence_sums[dx] / v),
                "specialists": specialists_per_dx[dx],
            }
            for dx, v in vote_counts.items()
        ],
        key=lambda x: (x["votes"], x["avg_confidence"]),
        reverse=True,
    )

    return {"ranked": ranked[:5], "top": ranked[0] if ranked else None}


async def run_debate_cli(case: str) -> dict:
    """Run the full debate and print results to stdout."""
    SEP = "=" * 65

    print(f"\n{SEP}")
    print(f"  MEDDEBATE  |  {len(SPECIALISTS)} AI Specialists Analyzing Case")
    print(f"{SEP}\n")

    # ── Phase 1 ──────────────────────────────────────────────────────
    print("PHASE 1: Independent Reasoning (running in parallel...)\n")
    queue1: asyncio.Queue = asyncio.Queue()
    tasks1 = [asyncio.create_task(reason_phase1(s, case, queue1)) for s in SPECIALISTS]

    phase1_results = []
    for _ in SPECIALISTS:
        item = await queue1.get()
        r = item["data"]
        phase1_results.append(r)
        s = r["specialist"]
        print(f"  {s['emoji']}  {s['name']:25s} [{s['title']:22s}]  "
              f"→ {r.get('diagnosis', '?'):35s}  {r.get('confidence', 0)}%")

    await asyncio.gather(*tasks1, return_exceptions=True)

    # ── Phase 2 ──────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("PHASE 2: Debate & Challenge (running in parallel...)\n")
    queue2: asyncio.Queue = asyncio.Queue()
    tasks2 = [
        asyncio.create_task(debate_phase2(s, case, phase1_results, queue2))
        for s in SPECIALISTS
    ]

    phase2_results = []
    for _ in SPECIALISTS:
        item = await queue2.get()
        r = item["data"]
        if not r:
            continue
        phase2_results.append(r)
        s = r["specialist"]
        if r.get("changed_mind"):
            print(f"  🔄 {s['name']} CHANGED MIND → {r['final_diagnosis']}")
        for c in r.get("challenges", []):
            print(f"  ⚔️  {s['name']} challenges {c['doctor']}: {c['reason'][:70]}...")
        for ag in r.get("agrees_with", []):
            print(f"  ✓  {s['name']} agrees with {ag}")

    await asyncio.gather(*tasks2, return_exceptions=True)

    # ── Consensus ─────────────────────────────────────────────────────
    consensus = compute_consensus(phase2_results)
    print(f"\n{SEP}")
    print("CONSENSUS DIAGNOSIS")
    print(f"{SEP}")
    for i, dx in enumerate(consensus["ranked"], 1):
        bar = "█" * (dx["avg_confidence"] // 10) + "░" * (10 - dx["avg_confidence"] // 10)
        print(
            f"  {i}. {dx['diagnosis']:40s}  {bar}  {dx['avg_confidence']}%  "
            f"({dx['votes']} votes)"
        )
    print()

    return {"phase1": phase1_results, "phase2": phase2_results, "consensus": consensus}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MedDebate - AI medical diagnosis debate")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case", help="Patient case description")
    group.add_argument("--demo", choices=list(DEMO_CASES.keys()), help="Load a demo case")
    args = parser.parse_args()

    if args.demo:
        demo = DEMO_CASES[args.demo]
        print(f"\n  Loading demo: {demo['title']}")
        case_text = demo["description"]
    else:
        case_text = args.case

    asyncio.run(run_debate_cli(case_text))
