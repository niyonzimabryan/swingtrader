# Brief 5 — carry the research over

Branch `claude/research-export` off `main` of `<RR>`. Depends on the local
agent having run `sync_research_mirror.py` (LOCAL_AGENT_PROMPT.md step 4) and
the owner having pushed the refreshed `research/` to swingtrader (or handed
the worker the files).

## Contract
1. Copy `research/` from swingtrader into `research/` here, one directory per
   ticker, mapped onto the template from brief 1 (dossier / thesis /
   invalidators; journal entries into `journal.md` with their dates).
2. Fill `research/INDEX.md` from what was copied.
3. Write one learning-record: what the previous system's research on BE (and
   any other name) got right, from the dossier's own evidence — sourced, or
   marked unknown.
4. Nothing is invented. A dossier section with no source is copied with a
   `source: none recorded` marker, not filled in.

## Acceptance
INDEX rows equal directories; CI green.
