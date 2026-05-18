---
name: brainpack-skills
description: Compress all SKILL.md files across Claude Code. Discovers skills, plugins, project-local. Stores originals in Brain.
---

Discover and compress every SKILL.md in the Claude ecosystem.

1. **Find all:** `find ~/.claude -name "SKILL.md" -type f` — skip paths matching `site-packages|node_modules|__pycache__|\.venv|/env/`. Also check `.claude/skills` in cwd. For each: check size, detect format (`head -10 | grep -q "%bp"` → bp or md)
2. **Report:** table of format|size|path sorted by size desc. Show totals, count packed vs unpacked. Skip files < 500 bytes
3. **Ask:** A) Compress all uncompressed (recommended) B) Only > 5KB C) Pick specific D) Skip
4. **Compress each:** Read → extract frontmatter → `s(namespace="skills", target="brainpack", data={text, chunk_id: "skill:<slug>", tags: ["skill","brainpack-backup"]})` → prepend frontmatter if stripped → write back → log sizes. Parallelize in batches of 5
5. **Summary:** files compressed, bytes before/after, % reduction. Originals in Brain for recovery. Re-run after plugin updates
