---
name: brain-setup
description: First-time Brain setup. Verify connection, check namespace, explain auth modes, offer skill compression.
---

1. Verify connection: `a(action="health")` — warn if any stores are down, continue regardless
2. Check namespace: `r(namespace=$BRAIN_NAMESPACE, query="project context")` — report what Brain already knows about this project
3. Explain auth surfaces — tell the user which applies to their use case:
   - **MCP / Claude Code (primary)**: Authenticated via OIDC OAuth. Brain handles token refresh automatically — sessions stay active without re-login prompts.
   - **OpenAI-compatible API** (`/api/v1/openai/`): For LLM tools that support OpenAI-compatible endpoints. Pass your OIDC access token as `Authorization: Bearer <token>`. Token comes from your OIDC provider — same login as Claude Code.
   - **Runners & scripts** (`/api/v1/*`): For CI, automation, or headless scripts. Go to the **portal → API Keys → Generate Key**. Use the issued credentials as Basic auth: `Authorization: Basic <base64(client_id:secret)>`. Credentials are long-lived; rotate from the portal if compromised.
4. Scan for uncompressed skills: `find ~/.claude -name "SKILL.md" -type f` — if any exist, offer `/brainpack-skills`
5. Done: tell the user "Brain is ready. Sessions auto-save, Brain-before-internet is active."
