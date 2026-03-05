---
name: clawhouse
description: 20 AI medical specialists debate a patient case and reach a consensus diagnosis. Powered by Claude claude-sonnet-4-6 agents running in parallel.
homepage: "https://github.com/yourusername/clawhouse"
user-invocable: true
metadata:
  openclaw:
    requires:
      bins: ["uv", "python3"]
      env: ["ANTHROPIC_API_KEY"]
    primaryEnv: "ANTHROPIC_API_KEY"
---

# ClawHouse Skill

Simulate a hospital consultation room where 20 AI medical specialists simultaneously analyze a patient case, debate each other's diagnoses, and reach a consensus.

## When to Invoke

Invoke this skill when the user:
- Describes a patient case with symptoms, history, or lab results
- Says "clawhouse [case]" or "/clawhouse [case]"
- Asks to "diagnose this case" or "get specialist opinions on"
- Wants to load a demo with "clawhouse demo:lupus", "clawhouse demo:wilson", "clawhouse demo:lyme", "clawhouse demo:lead"

## How to Run

### For a custom case:
```
exec command:"cd {skill_dir} && uv run python scripts/debate_engine.py --case '{escaped_case}'"
```

### For a demo case:
```
exec command:"cd {skill_dir} && uv run python scripts/debate_engine.py --demo lupus"
```

Available demos: `lupus`, `wilson`, `lyme`, `lead`

## Before Running

Install dependencies if not already done:
```
exec command:"cd {skill_dir} && uv pip install anthropic"
```

## Output Interpretation

The script outputs three phases:
1. **Phase 1** — Each of 20 specialists provides their independent diagnosis + confidence
2. **Phase 2** — Specialists debate: challenges, agreements, mind changes
3. **Consensus** — Ranked differential diagnosis with vote counts and confidence

Present the consensus to the user prominently. Highlight any notable challenges from Dr. House.

## Important Notes

- This is for educational and demonstration purposes only
- NOT a substitute for real medical advice
- Always remind users to consult qualified medical professionals for actual healthcare decisions
- The debate showcases AI multi-agent reasoning, not clinical recommendations
