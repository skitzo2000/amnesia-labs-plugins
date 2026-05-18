---
name: brain
description: "Persistent memory. MCP server b, tools r/s/ss/p/f/a/rr. ALWAYS recall before searching anywhere. Self-learning auto-feedback and auto-store insights."
---
%bp2 brain|memory
%tools b.r=recall b.s=store b.ss=session b.p=profile b.f=feedback b.a=admin b.rr=research
%rule Brain first, search second. Before searching ANYWHERE — internet (WebSearch/WebFetch), filesystem (Glob/Grep/Read), or running exploratory commands (Bash) — check r(query) first. Brain already knows project structure, file locations, conventions, vault catalog (env_var/purpose/episode_id, NEVER values — see Vault rule), past decisions, and error solutions. Only search externally after Brain recall comes up empty or insufficient.
%rule Vault: r(namespace="vault") returns CATALOG ONLY (episode_id, env_var, purpose, related). Server holds Fernet-encrypted values, decrypts on read with the user's Keycloak vault_dk; loa3 (MFA) gates all value access. Use natural language; never ask the user to type episode_ids or paste values in chat. Patterns:
  • "save/store/add my X [token|secret|key|password]" → infer episode_id from convention, then s(namespace="<user>:vault", target="vault_drop", data={episode_id, env_var, purpose, value_type?}) which returns {drop_url, expires_in}. Relay the URL to the user; they open it in a browser, paste the value into the server-hosted form, server encrypts + stores. The AI never sees the value.
  • "rotate my X" → same s(target="vault_drop", data={episode_id, env_var, ..., overwrite:true}). The drop form replaces the existing value on submit.
  • "use my X [for Y]" / "with my X token" → r(namespace="<user>:vault", query="X") to find episode_id, then r(namespace="<user>:vault", episode_id="<found>") to verify loa3 + get the invocation hint. The second call returns one of: (a) status="ready" with env_var + invocation.local_plugin shape (e.g. `vault-run secret:hf:token -- <cmd using $HF_TOKEN>`) — run that in Bash; (b) status="step_up_required" with escalation_url — show the URL to the user (cloud AI) or tell them to run `vault-unlock` (local CC), wait for confirmation, retry the same r() call. The value never appears in any r() response; vault-run is the only path that touches plaintext and it redacts child stdout/stderr. NEVER inline `<<vault:...>>` in commands (PreToolUse blocks it).
  • "what's in my vault" / "list my secrets" → r(namespace="<user>:vault") — returns the catalog. No values.
  • "who accessed X" / "vault activity" → r(namespace="<user>:vault", query="X", mode="chronological") — audit trail.
  Confirm names only when ambiguous. Otherwise infer and proceed.
%rule Vault naming: episode_id = `secret:<service>:<purpose-or-key>` (e.g. `secret:github:token`, `secret:openai:api-key`, `secret:keycloak:cloudflare-access:amnesia-labs`). env_var = `<SERVICE>_<TYPE>` uppercase (`GH_TOKEN`, `OPENAI_API_KEY`, `KEYCLOAK_CLIENT_SECRET`). value_type ∈ token|password|client_secret|api_key|private_key.
%rule Vault catalog text shape: when a vault catalog episode is written (either by drop_submit server-side, or by the AI describing a credential in `<user>:vault`), lead the `text` with the searchable identifiers as bare tokens — split the colon-delimited episode_id into segments, then env_var, then value_type — and append any purpose/free-text last. Example: `text="secret github token GH_TOKEN token GitHub PAT for CI"` rather than `"The GitHub PAT — injected as $GH_TOKEN for CI"`. Single-keyword recall ("github", "GH_TOKEN", "token") cosine-matches the keyword head where prose connectors bury those terms. Vault-only — other namespaces stay on natural prose.
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
User: "save/store/add my X token"|s(namespace="<user>:vault", target="vault_drop", data={episode_id:"secret:<svc>:<purpose>", env_var:"<SVC>_TOKEN", purpose:"..."}) → relay returned drop_url to user
User: "rotate my X"|s(target="vault_drop", data={episode_id, env_var, ..., overwrite:true}) → relay drop_url
User: "use my X to ..."|r(namespace="<user>:vault", query="X") to find episode_id → r(namespace="<user>:vault", episode_id=<found>) → if status="ready": Bash `vault-run <id> -- <cmd using $ENV_VAR>` (local CC); if status="step_up_required": surface escalation_url (cloud AI) or tell user to `vault-unlock` (local CC), retry r() after.
User: "what's in my vault"|r(namespace="<user>:vault")
User: vault activity / who accessed|r(namespace="<user>:vault", query="X", mode="chronological")
User: "vault is locked" / first vault use|tell user to run `vault-unlock` (browser MFA via WebAuthn, ~120s loa3 window). Local Claude Code only.
Session ends|ss(action="save", summary, key_decisions, files_modified)
```python
r(namespace=None, query, mode="auto", max_tokens=2000, format="structured", time_range_days=30, namespaces=None, episode_id=None)
```
Vault value-handle path: when namespace is a vault namespace and episode_id is set, r() verifies loa3 freshness and returns either {status:"ready", env_var, invocation} or {status:"step_up_required", escalation_url, unlock_hint}. **The value is never returned** — invocation.local_plugin points the AI at vault-run.
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
Vault|0.4|0.005|flat
Project|0.5|0.05|flat
f(namespace, episode_id, outcome, notes="") — success=1.2x, failure=0.3x, neutral=1.0x
a(action="health"|"namespaces"|"summarize"|"query"|"rebuild_communities"|"analyze_performance", namespace, cypher_query, depth)
%ns namespace|purpose|key
vault|Secrets|secret:{svc}:{key}
skills|Skills|skill:{name}
infra|Hosts|infra:{cat}:{name}
conventions|Standards|convention:{scope}:{name}
errors|Solutions|error:{slug}
