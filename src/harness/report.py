"""Markdown reports across runs, from the DuckDB store.

Referee scores are only ever compared within one (rubric version, referee model) pair.
"""

from __future__ import annotations

import json
from collections import defaultdict

from harness.docsmap import normalize_url
from harness.referee.framework import similar
from harness.store import Store


def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |")
    return "\n".join(out) + "\n"


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _where(filters: dict[str, str | None]) -> tuple[str, list]:
    clauses, params = [], []
    for k, v in filters.items():
        if v is not None:
            clauses.append(f"r.{k} = ?")
            params.append(v)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def build(store: Store, *, report_facts: list[str], **filters: str | None) -> str:
    where, params = _where(filters)
    runs = store.query(f"SELECT * FROM runs r{where} ORDER BY started_at", params)
    ids = [r["run_id"] for r in runs]
    if not ids:
        return "# Harness report\n\nNo runs match.\n"
    idlist = ",".join("?" * len(ids))
    facts = defaultdict(dict)
    for f in store.query(f"SELECT run_id, key, value FROM facts WHERE run_id IN ({idlist})", ids):
        facts[f["run_id"]][f["key"]] = json.loads(f["value"]) if f["value"] is not None else None

    parts = ["# Harness report\n", f"{len(runs)} runs.\n"]

    parts.append("## Runs\n")
    rows = []
    for r in runs:
        row = {
            "run": r["run_id"],
            "agent": r["agent"],
            "repo": r["repo"],
            "bundle": f"{r['bundle']}@{r['bundle_version']}",
            "tool": r["tool_version"],
            "cloud": r["cloud"],
            "status": r["status"],
            "passed": r["passed"],
            "retries": r["retries_needed"],
            "cost": r["cost_usd"],
            "min": (r["duration_s"] or 0) / 60,
            "turns": r["turns"],
        }
        for key in report_facts:
            row[key] = facts[r["run_id"]].get(key)
        rows.append(row)
    parts.append(_table(rows, list(rows[0].keys())))
    exposed = [r["run_id"] for r in runs if facts[r["run_id"]].get("harness_exposure")]
    if exposed:
        parts.append(
            "\n**Harness exposure:** these runs' transcripts mention the harness by name, so their "
            "results may be contaminated (see the `harness_exposure` fact): " + ", ".join(exposed) + "\n"
        )

    parts.append("\n## Cells\n")
    cells = store.query(
        f"""SELECT agent, repo, bundle, bundle_version, tool_version, cloud, count(*) AS n,
                   avg(CASE WHEN passed THEN 1 ELSE 0 END) AS pass_rate,
                   avg(cost_usd) AS mean_cost, avg(turns) AS mean_turns,
                   avg(retries_needed) AS mean_retries
            FROM runs r{where} GROUP BY ALL ORDER BY ALL""",
        params,
    )
    parts.append(_table(cells, list(cells[0].keys())))

    parts.append("\n## Referee scores\n")
    versions = store.query(
        f"SELECT DISTINCT rubric_version, referee_model FROM scores WHERE run_id IN ({idlist}) ORDER BY ALL",
        ids,
    )
    if not versions:
        parts.append("_no scorecards yet_\n")
    for v in versions:
        parts.append(f"\n### rubric {v['rubric_version']} · {v['referee_model']}\n")
        rows = store.query(
            f"""SELECT r.agent, r.bundle, s.criterion, count(*) AS n, avg(s.score) AS mean,
                       min(s.score) AS min, max(s.score) AS max
                FROM scores s JOIN runs r USING (run_id)
                WHERE s.run_id IN ({idlist}) AND s.rubric_version = ? AND s.referee_model = ?
                GROUP BY ALL ORDER BY ALL""",
            [*ids, v["rubric_version"], v["referee_model"]],
        )
        parts.append(_table(rows, ["agent", "bundle", "criterion", "n", "mean", "min", "max"]))

    parts.append("\n## Friction by root cause\n")
    rows = store.query(
        f"""SELECT root_cause, count(*) AS n, count(DISTINCT run_id) AS runs
            FROM findings WHERE section = 'friction' AND run_id IN ({idlist})
            GROUP BY ALL ORDER BY n DESC""",
        ids,
    )
    parts.append(_table(rows, ["root_cause", "n", "runs"]))
    rows = store.query(
        f"""SELECT run_id, root_cause, summary FROM findings
            WHERE section = 'friction' AND run_id IN ({idlist}) ORDER BY root_cause""",
        ids,
    )
    if rows:
        parts.append("\n" + "\n".join(f"- **{r['root_cause']}** ({r['run_id']}): {r['summary']}" for r in rows) + "\n")

    parts.append("\n## Hallucinated APIs and legacy confusion\n")
    rows = store.query(
        f"""SELECT section, summary, count(*) AS n FROM findings
            WHERE section IN ('hallucinated_apis','legacy_confusion') AND run_id IN ({idlist})
            GROUP BY ALL ORDER BY n DESC""",
        ids,
    )
    parts.append(_table(rows, ["section", "summary", "n"]))

    parts.append("\n## CI gaps\n")
    rows = store.query(
        f"SELECT run_id, summary FROM findings WHERE section = 'ci_gaps' AND run_id IN ({idlist})", ids
    )
    parts.append(_table(rows, ["run_id", "summary"]))

    parts.append("\n## Self-report claims\n")
    rows = store.query(
        f"""SELECT verdict, count(*) AS n FROM findings WHERE section = 'claims' AND run_id IN ({idlist})
            GROUP BY ALL ORDER BY n DESC""",
        ids,
    )
    parts.append(_table(rows, ["verdict", "n"]))

    parts.append("\n## Issue candidates (deduped, for review)\n")
    parts.append(_issues(store, ids, idlist))

    parts.append("\n## Docs map\n")
    parts.append(_docs(store, ids, idlist))

    parts.append("\n## Calibration\n")
    parts.append(calibration_table(store, ids))
    return "\n".join(parts)


def _issues(store: Store, ids: list[str], idlist: str) -> str:
    rows = store.query(
        f"SELECT fingerprint, title, body, category, run_id FROM issues WHERE run_id IN ({idlist})", ids
    )
    groups: list[dict] = []
    for r in rows:
        for g in groups:
            if g["fingerprint"] == r["fingerprint"] or (
                g["category"] == r["category"] and similar(g["title"], r["title"])
            ):
                g["runs"].add(r["run_id"])
                break
        else:
            groups.append({**r, "runs": {r["run_id"]}})
    if not groups:
        return "_none_\n"
    groups.sort(key=lambda g: -len(g["runs"]))
    out = []
    for g in groups:
        out.append(f"### {g['title']}\n\n_{g['category']} · {len(g['runs'])} run(s): {', '.join(sorted(g['runs']))}_\n\n{g['body']}\n")
    return "\n".join(out)


def _docs(store: Store, ids: list[str], idlist: str) -> str:
    top = store.query(
        f"""SELECT url, source, doc_version, count(*) AS visits, count(DISTINCT run_id) AS runs,
                   sum(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) AS error_after,
                   sum(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS success_after
            FROM docs WHERE run_id IN ({idlist}) AND kind != 'local'
            GROUP BY ALL ORDER BY visits DESC LIMIT 40""",
        ids,
    )
    out = ["### Most-used pages\n", _table(top, ["url", "source", "doc_version", "visits", "runs", "error_after", "success_after"])]
    versions = store.query(
        f"""SELECT source, coalesce(doc_version, '?') AS doc_version, count(*) AS visits
            FROM docs WHERE run_id IN ({idlist}) GROUP BY ALL ORDER BY ALL""",
        ids,
    )
    out += ["\n### Doc versions\n", _table(versions, ["source", "doc_version", "visits"])]
    sitemap = store.query("SELECT source, url FROM sitemap")
    if sitemap:
        visited = {normalize_url(r["url"]) for r in store.query("SELECT url FROM docs")}
        by_source = defaultdict(list)
        for r in sitemap:
            if normalize_url(r["url"]) not in visited:
                by_source[r["source"]].append(r["url"])
        out.append("\n### Never visited (from sitemaps)\n")
        for src, urls in by_source.items():
            total = sum(1 for r in sitemap if r["source"] == src)
            out.append(f"- **{src}**: {len(urls)} of {total} pages never visited")
    return "\n".join(out) + "\n"


def calibration_table(store: Store, ids: list[str] | None = None) -> str:
    cond = f"AND h.run_id IN ({','.join('?' * len(ids))})" if ids else ""
    rows = store.query(
        f"""SELECT s.rubric_version, s.referee_model, s.criterion, count(*) AS n,
                   avg(abs(s.score - h.score)) AS mae,
                   avg(CASE WHEN s.score = h.score THEN 1 ELSE 0 END) AS exact,
                   avg(CASE WHEN abs(s.score - h.score) <= 1 THEN 1 ELSE 0 END) AS within_1
            FROM human_scores h JOIN scores s
              ON s.run_id = h.run_id AND s.criterion = h.criterion AND s.rubric_version = h.rubric_version
            WHERE 1=1 {cond}
            GROUP BY ALL ORDER BY ALL""",
        ids or [],
    )
    return _table(rows, ["rubric_version", "referee_model", "criterion", "n", "mae", "exact", "within_1"])
