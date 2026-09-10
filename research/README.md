# `research/` — the narrative mirror

Generated. **Do not hand-edit these files** except through
`python -m scripts.sync_research_mirror --import`, which reads prose back
through the same validation the tools use.

The workspace's structured store is Postgres; this directory is the second half
of the design (Spec K §3.3). Postgres gives querying, staleness and joins; git
gives durability, diffability, review, and an **offline fallback** — if the
workspace API is down, a session that has cloned this repo still has every
thesis, every invalidator, and every past decision. Just not live prices.

```
companies/<TICKER>.md            dossier sections, with their sources and warnings
theses/<YYYY-MM>-<ticker>-<slug>.md   front matter carries status, probability, invalidators
journal/<YYYY-MM>.md             decisions, the budget each drew from, cohort ids
questions.md                     open research questions
```

**This directory is deliberately incomplete.** The repo is public and vendor
licences forbid redistribution, so content whose source tier is news, vendor
data, or a scraped page never reaches a file here. It appears as

```
<!-- withheld: news-derived, see dossier_sections/41 -->
```

and lives only in Postgres. `--import` reads that marker as "keep the database
copy", so re-importing never blanks the original.

Written by `scripts/sync_research_mirror.py`. See
[docs/RESEARCH_WORKSPACE.md](../docs/RESEARCH_WORKSPACE.md).
