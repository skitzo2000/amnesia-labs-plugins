---
name: brainpack
description: BrainPack compress files. /brainpack <path|all|*|plugin-name>. Auto-runs, no prompts.
---

Parse $ARGUMENTS and execute automatically:
- Empty: error "Usage: /brainpack <path|all|*|plugin-name>"
- `all` or `*`: invoke /brain:brainpack-skills
- File path: compress that file
- Other: `find ~/.claude -path "*$ARGUMENTS*" -name "SKILL.md"` and compress all matches

Compress each file:
1. Read. Skip if `head -10 | grep -q "%bp"` (already packed)
2. Extract YAML frontmatter
3. `s(namespace="skills", target="brainpack", data={text: content, chunk_id: "brainpack:filename"})`
4. Prepend original frontmatter if stripped
5. Write back. Report before/after bytes per file
6. Skip if < 10% savings
