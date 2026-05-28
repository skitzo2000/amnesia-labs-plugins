---
name: brain
description: "Persistent memory. MCP server b, tools r/s/ss/p/f/a/rr. ALWAYS recall before searching anywhere. Self-learning auto-feedback and auto-store insights."
---
%bp2 brain|memory
%tools b.r=recall b.s=store b.ss=session b.p=profile b.f=feedback b.a=admin b.rr=research
%rule Brain first, search second. Before searching ANYWHERE — internet (WebSearch/WebFetch), filesystem (Glob/Grep/Read), or running exploratory commands (Bash) — check r(query) first. Brain already knows project structure, file locations, conventions, past decisions, and error solutions. (Secrets live in the vault — see the separate vault skill.) Only search externally after Brain recall comes up empty or insufficient.
%rule Cross-domain: use mode="discover" or namespaces=["ns1","ns2"] when answer may exist in another domain.
%rule Namespace-free: omit namespace to search all accessible non-protected namespaces. Results grouped by namespace. Drill into specific namespace for full content + sensory data.
%rule Self-learning: ALWAYS close the feedback loop. After using recalled content, call f() with outcome. After solving a hard problem, store the insight. After hitting a novel error, store to errors namespace. After learning a convention, store to conventions. This is not optional.
%rule Empty recall: if r() returns nothing useful, note the gap. After finding the answer (from code, internet, or debugging), store it so next recall succeeds.
%rule Store failures: if s() fails, check data keys against target schema. Store the fix to conventions if the error was non-obvious.
%rule Self-save before stop: when a session produced real decisions (architecture choice, design tradeoff, method picked, PR opened), call ss(action="save", key_decisions=[...]) explicitly before ending. The Stop hook is a server-side-extraction fallback — useful, but agent-curated decisions always beat heuristic ones, and they feed the project rollup that primes future sessions.
%rule First-session context size: when the SessionStart hook injects "FIRST-TIME SETUP" guidance with `context_size_prompt_needed`, ask the user once: "How much project context on session start? 0=none, 1=light(1000), 2=medium(2000), 3=heavy(5000), or a custom integer 1..20000." Then call ss(action="start", context_size=<their choice>) — the value persists on the profile, so this prompt only fires once per namespace. Users can reset later via p(action="set", profile_data={"context_brief_preset_set": false}).
%when moment|tool
Session starts|ss(action="start", namespace, session_id, project_path, goals, plugin_version=<from local plugin.json>)
ss() response has plugin_update?|If plugin_update.available=true, tell user: "Brain plugin update available: v{current} → v{latest}. Run /brain-update to install."
Need context|r(query) or r(namespace, query) if you know the namespace
Before ANY search|r(query) first — omit namespace to search everything accessible
Don't know where something is|r() before find/ls/grep — Brain knows file locations, project structure, past work
Learn something|s(namespace, target="episodic", data={episode_id, text})
Discover pattern|s(target="concept") + s(target="link")
Key decision|ss(action="save", key_decisions=[...])
Recalled method worked|f(namespace, episode_id, outcome="success")
Recalled method failed|f(namespace, episode_id, outcome="failure", notes=why)
Novel error solved|s(namespace="errors", target="episodic", data={episode_id="error:slug", text=problem+solution})
Learned a convention|s(namespace="conventions", target="brainpack", data={text, chunk_id})
Found answer internet had|s() to fill the knowledge gap Brain missed
Compress text|s(target="brainpack", data={text, chunk_id})
Secret/token/credential mentioned|see the separate vault skill
Session ends|ss(action="save", summary, key_decisions, files_modified)
```python
r(namespace=None, query, mode="auto", max_tokens=2000, format="structured", time_range_days=30, namespaces=None, episode_id=None)
```
Modes: auto, episodic, semantic, chronological, sensory, discover
Formats: structured, text, minimal, packed
Scoring: final_score = similarity * (1/(1+decay_rate*age_days)) * outcome_weight
%targets target|required|optional
episodic|episode_id|text, embedding, metadata, timestamp
brainpack|text|task_context, token_budget, chunk_id, tags, model
sensory|stream_key, data|maxlen
chronological|event_type, source, data|metadata, timestamp
concept|concept_name|properties
link|from_concept, to_concept, relationship_type|weight, properties
cross_reference|from_concept, to_namespace, to_concept|reason, weight
Use parent_chunks=True for long content. All targets validate keys.
%profiles pattern|score_threshold|decay_rate|retrieval_mode
Long docs|0.3|0.01|parent-child
Project|0.5|0.05|flat
f(namespace, episode_id, outcome, notes="") — success=1.2x, failure=0.3x, neutral=1.0x
a(action="health"|"namespaces"|"summarize"|"query"|"rebuild_communities"|"analyze_performance", namespace, cypher_query, depth)
%ns namespace|purpose|key
skills|Skills|skill:{name}
infra|Hosts|infra:{cat}:{name}
conventions|Standards|convention:{scope}:{name}
errors|Solutions|error:{slug}
