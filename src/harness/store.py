"""DuckDB store: an index over run bundles for cross-run reports.

Bundles on disk are the source of truth; ``harness store rebuild`` re-ingests all of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from harness.analysis import load_facts
from harness.bundle import RunBundle

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, pack VARCHAR, repo VARCHAR, bundle VARCHAR,
    bundle_version VARCHAR, bundle_hash VARCHAR, agent VARCHAR, model VARCHAR,
    tool_version VARCHAR, cloud BOOLEAN, status VARCHAR, slot VARCHAR, repo_sha VARCHAR,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, passed BOOLEAN, verify_attempts INTEGER,
    retries_needed INTEGER, cost_usd DOUBLE, duration_s DOUBLE, turns INTEGER,
    pushed BOOLEAN, versions JSON, path VARCHAR
);
CREATE TABLE IF NOT EXISTS facts (run_id VARCHAR, key VARCHAR, value JSON, detail VARCHAR);
CREATE TABLE IF NOT EXISTS scores (
    run_id VARCHAR, rubric_version VARCHAR, referee_model VARCHAR, referee_prompt_version VARCHAR,
    criterion VARCHAR, score INTEGER, rationale VARCHAR, evidence JSON
);
CREATE TABLE IF NOT EXISTS findings (
    run_id VARCHAR, rubric_version VARCHAR, referee_model VARCHAR, section VARCHAR,
    summary VARCHAR, detail VARCHAR, root_cause VARCHAR, verdict VARCHAR, evidence JSON
);
CREATE TABLE IF NOT EXISTS issues (
    run_id VARCHAR, fingerprint VARCHAR, title VARCHAR, body VARCHAR, category VARCHAR,
    evidence JSON
);
CREATE TABLE IF NOT EXISTS docs (
    run_id VARCHAR, event VARCHAR, url VARCHAR, source VARCHAR, doc_version VARCHAR,
    kind VARCHAR, outcome VARCHAR
);
CREATE TABLE IF NOT EXISTS human_scores (
    run_id VARCHAR, rater VARCHAR, rubric_version VARCHAR, criterion VARCHAR, score INTEGER
);
CREATE TABLE IF NOT EXISTS sitemap (source VARCHAR, url VARCHAR);
"""

PER_RUN_TABLES = ("runs", "facts", "scores", "findings", "issues", "docs", "human_scores")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.con.close()

    def ingest(self, bundle: RunBundle) -> None:
        meta = bundle.meta()
        rid = meta.run_id
        self.con.execute("BEGIN")
        for t in PER_RUN_TABLES:
            self.con.execute(f"DELETE FROM {t} WHERE run_id = ?", [rid])
        facts = {f.key: f.value for f in load_facts(bundle)}
        self.con.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                rid, meta.spec.pack, meta.spec.repo, meta.spec.bundle, meta.bundle_version,
                meta.bundle_hash, meta.spec.agent.value, meta.spec.model,
                meta.versions.get("tool") or meta.spec.tool_version, meta.spec.cloud,
                meta.status.value, meta.slot, meta.repo_sha, meta.started_at, meta.finished_at,
                facts.get("verify.passed"), facts.get("verify.attempts"),
                facts.get("retries_needed"), facts.get("cost_usd"), facts.get("duration_s"),
                facts.get("turns"), facts.get("pushed"), json.dumps(meta.versions),
                str(bundle.path),
            ],
        )
        for f in load_facts(bundle):
            self.con.execute(
                "INSERT INTO facts VALUES (?,?,?,?)", [rid, f.key, json.dumps(f.value), f.detail]
            )
        card = bundle.scorecard()
        if card:
            tag = [card.rubric_version, card.referee_model]
            for s in card.scores:
                self.con.execute(
                    "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)",
                    [rid, *tag, card.referee_prompt_version, s.criterion, s.score, s.rationale,
                     json.dumps([e.model_dump() for e in s.evidence])],
                )
            sections = {
                "ci_gaps": card.ci_gaps,
                "hallucinated_apis": card.hallucinated_apis,
                "legacy_confusion": card.legacy_confusion,
                "docs_judgments": card.docs_judgments,
            }
            for section, items in sections.items():
                for f in items:
                    self._finding(rid, tag, section, f.summary, f.detail, None, None, f.evidence)
            for fr in card.friction:
                self._finding(rid, tag, "friction", fr.summary, "", fr.root_cause.value, None, fr.evidence)
            for c in card.claims:
                self._finding(rid, tag, "claims", c.claim, "", None, c.verdict, c.evidence)
            for i in card.issue_candidates:
                self.con.execute(
                    "INSERT INTO issues VALUES (?,?,?,?,?,?)",
                    [rid, i.fingerprint, i.title, i.body, str(i.category),
                     json.dumps([e.model_dump() for e in i.evidence])],
                )
        for v in bundle.read_json("docs_map.json") or []:
            self.con.execute(
                "INSERT INTO docs VALUES (?,?,?,?,?,?,?)",
                [rid, v["event"], v["url"], v["source"], v.get("doc_version"), v["kind"], v["outcome"]],
            )
        human = bundle.human_scores()
        if human:
            for crit, score in human.scores.items():
                self.con.execute(
                    "INSERT INTO human_scores VALUES (?,?,?,?,?)",
                    [rid, human.rater, human.rubric_version, crit, score],
                )
        self.con.execute("COMMIT")

    def _finding(self, rid, tag, section, summary, detail, root_cause, verdict, evidence) -> None:
        self.con.execute(
            "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?)",
            [rid, *tag, section, summary, detail, root_cause, verdict,
             json.dumps([e.model_dump() for e in evidence])],
        )

    def set_sitemap(self, source: str, urls: list[str]) -> None:
        self.con.execute("DELETE FROM sitemap WHERE source = ?", [source])
        self.con.executemany("INSERT INTO sitemap VALUES (?, ?)", [[source, u] for u in urls])

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
