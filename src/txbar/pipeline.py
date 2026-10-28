"""Pipeline Orchestrator for Texas Bar attorney enrichment.

Handles:
- Incremental batch loading from storage.
- Checkpointing for interruption tolerance.
- Entity resolution and multi-signal scoring.
- Record-level and run-level Data Quality gates.
- Export of delivery artifacts: enriched.csv (for Jose), enriched.ndjson (for BigQuery), and run_report.json.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from txbar.client import TexasBarClient
from txbar.config import Settings, get_settings
from txbar.matching import MatchEngine
from txbar.normalize import normalize_phone
from txbar.provider import DirectoryProvider
from txbar.quality import evaluate_run_quality_gate, validate_enriched_record
from txbar.storage import StorageBackend, get_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def format_delivery_dataframe(res_df: pl.DataFrame) -> pl.DataFrame:
    """Format a match_results DataFrame into the clean enriched CSV delivery structure."""
    delivery_cols = [
        # Original 8 columns
        "input_name",
        "input_firm",
        "input_title",
        "input_practice",
        "input_city",
        "input_state",
        "input_jd_year",
        "input_email",
        # Texas Bar Enriched Fields
        "contact_id",
        "bar_card_number",
        "full_name",
        "preferred_name",
        "license_status",
        "license_date",
        "current_firm",
        "firm_size",
        "phone",
        "address",
        "practice_city",
        "practice_areas",
        "law_school",
        "grad_year",
        "profile_url",
        # 4 Audit & Transparency Columns
        "match_score",
        "match_status",
        "match_explanation",
        "firm_changed",
        "review_status",
    ]
    available_cols = [c for c in delivery_cols if c in res_df.columns]
    delivery_df = res_df.select(available_cols)
    clean_renames = {
        "input_name": "Name",
        "input_firm": "Original_Firm",
        "input_title": "Title",
        "input_practice": "Practice",
        "input_city": "City",
        "input_state": "State",
        "input_jd_year": "Original_JD_Year",
        "input_email": "Email",
        "current_firm": "Verified_Firm",
        "match_score": "Match_Confidence_Score",
        "match_status": "Match_Status",
        "match_explanation": "Audit_Explanation",
        "firm_changed": "Firm_Change_Detected",
    }
    return delivery_df.rename({k: v for k, v in clean_renames.items() if k in delivery_df.columns})


class Pipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        storage: StorageBackend | None = None,
        provider: DirectoryProvider | None = None,
        resolver: MatchEngine | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.storage = storage or get_storage(self.settings)
        self.provider = provider
        self.resolver = resolver

    def run(
        self,
        limit: int | None = None,
        seed_if_empty: bool = True,
        input_csv: Path | None = None,
    ) -> dict[str, Any]:
        """Execute the enrichment pipeline."""
        run_id = f"run_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        started_at = datetime.now(UTC)
        logger.info("Starting enrichment pipeline run %s", run_id)

        # 1. Ensure source is seeded
        source_df = self.storage.load_source()
        if len(source_df) == 0 and seed_if_empty:
            seed_path = input_csv or (self.settings.resolve(Path("data/input/tx_lawyers.csv")))
            logger.info("Source table is empty. Seeding from %s...", seed_path)
            self.storage.seed_source(seed_path)
            source_df = self.storage.load_source()

        if len(source_df) == 0:
            logger.warning("No attorneys to process in attorneys_source.")
            return {"run_id": run_id, "status": "no_data"}

        attorneys = source_df.to_dicts()
        if limit:
            attorneys = attorneys[:limit]

        logger.info("Processing batch of %d attorneys...", len(attorneys))

        # Checkpoint directory
        chk_dir = self.settings.resolve(self.settings.output_dir / "checkpoints")
        chk_dir.mkdir(parents=True, exist_ok=True)
        chk_file = chk_dir / f"{run_id}.ndjson"

        all_candidates: list[dict[str, Any]] = []
        all_results: list[dict[str, Any]] = []

        now_utc = datetime.now(UTC)

        client_instance = self.provider or TexasBarClient(self.settings)
        with client_instance as client:
            engine = self.resolver or MatchEngine(provider=client, settings=self.settings)

            for idx, att in enumerate(attorneys, start=1):
                att_uuid = str(att["attorney_uuid"])
                name_val = att.get("name") or ""
                logger.info(
                    "[%d/%d] Resolving '%s' (%s, %s)...",
                    idx,
                    len(attorneys),
                    name_val,
                    att.get("firm"),
                    att.get("city"),
                )

                decision = engine.resolve_attorney(att)
                best = decision.best_candidate
                prof = best.profile if best else None

                # Record candidates
                for c in decision.candidates:
                    all_candidates.append(
                        {
                            "run_id": run_id,
                            "attorney_uuid": att_uuid,
                            "contact_id": c.contact_id,
                            "search_stage": c.search_stage,
                            "candidate_rank": c.rank,
                            "score": c.score,
                            "score_name": c.score_name,
                            "score_firm": c.score_firm,
                            "score_temporal": c.score_temporal,
                            "score_geo": c.score_geo,
                            "full_name": c.full_name,
                            "preferred_name": c.preferred_name,
                            "firm": c.firm,
                            "city": c.city,
                            "bar_card_number": c.bar_card_number,
                            "license_date": c.license_date,
                            "license_status": c.license_status,
                            "law_school": c.law_school,
                            "grad_year": c.grad_year,
                            "explanation": c.explanation,
                            "scraped_at": now_utc,
                        }
                    )

                # Assemble result record
                phone_raw = prof.phone if prof else (best.profile.phone if best and best.profile else "")
                phone_norm = normalize_phone(phone_raw)

                result_rec: dict[str, Any] = {
                    "run_id": run_id,
                    "attorney_uuid": att_uuid,
                    "input_name": att.get("name"),
                    "input_firm": att.get("firm"),
                    "input_title": att.get("title"),
                    "input_practice": att.get("practice"),
                    "input_city": att.get("city"),
                    "input_state": att.get("state"),
                    "input_jd_year": att.get("jd_year"),
                    "input_email": att.get("email"),
                    "contact_id": best.contact_id if best else None,
                    "bar_card_number": prof.bar_card_number
                    if prof
                    else (best.bar_card_number if best else None),
                    "first_name": prof.name.first_name if prof else "",
                    "middle_name": prof.name.middle_name if prof else "",
                    "last_name": prof.name.last_name if prof else "",
                    "preferred_name": prof.name.preferred_name if prof else "",
                    "full_name": prof.name.full_name if prof else (best.full_name if best else ""),
                    "license_status": prof.license_status
                    if prof
                    else (best.license_status if best else None),
                    "license_date": prof.license_date.isoformat() if (prof and prof.license_date) else None,
                    "current_firm": prof.firm if prof else (best.firm if best else None),
                    "firm_size": prof.firm_size if prof else "",
                    "occupation": prof.occupation if prof else "",
                    "practice_areas": "|".join(prof.practice_areas) if prof else "",
                    "phone": phone_norm,
                    "address": prof.address if prof else "",
                    "practice_city": prof.city if prof else (best.city if best else ""),
                    "practice_state": prof.state if prof else "",
                    "practice_zip": prof.zip if prof else "",
                    "law_school": prof.law_school if prof else "",
                    "law_degree": prof.law_degree if prof else "",
                    "grad_year": prof.grad_year if prof else None,
                    "profile_url": f"https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer&template=/Customsource/MemberDirectory/MemberDirectoryDetail.cfm&ContactID={best.contact_id}"
                    if best
                    else None,
                    "match_score": decision.match_score,
                    "score_name": decision.score_name,
                    "score_firm": decision.score_firm,
                    "score_temporal": decision.score_temporal,
                    "score_geo": decision.score_geo,
                    "match_status": decision.match_status,
                    "match_explanation": decision.match_explanation,
                    "firm_changed": decision.firm_changed,
                    "candidates_count": len(decision.candidates),
                    "search_stage": decision.search_stage,
                    "dq_issues": "",
                    "review_status": decision.review_status,
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "review_note": None,
                    "scraped_at": now_utc,
                }

                # Record-level DQ validation
                dq_res = validate_enriched_record(result_rec)
                if not dq_res.is_valid:
                    result_rec["dq_issues"] = "|".join(dq_res.issues)

                all_results.append(result_rec)

                # Append checkpoint line
                with chk_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(result_rec, default=str) + "\n")

            metrics = getattr(client, "metrics", None)
            total_reqs = metrics.total_requests if metrics else len(all_results) * 2

        # 2. Evaluate batch-level Quality Gate
        dq_report = evaluate_run_quality_gate(all_results, self.settings)

        finished_at = datetime.now(UTC)
        run_status = "succeeded" if dq_report.gate_passed else "gated"

        firm_changes_count = sum(1 for r in all_results if r.get("firm_changed"))

        run_record = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": run_status,
            "trigger": "pipeline",
            "total": len(all_results),
            "auto_approved": dq_report.auto_approved,
            "pending_review": dq_report.pending_review,
            "not_found": dq_report.not_found,
            "errors": dq_report.errors,
            "firm_changes": firm_changes_count,
            "http_requests": total_reqs,
            "dq_gate_passed": dq_report.gate_passed,
            "dq_report": json.dumps(
                {
                    "violations": dq_report.gate_violations,
                    "missing_phone_ratio": dq_report.missing_phone_ratio,
                    "not_found_ratio": dq_report.not_found_ratio,
                    "rule_summary": dq_report.rule_summary,
                }
            ),
        }

        # 3. Save run to warehouse
        self.storage.save_run(run_record, all_candidates, all_results)

        # 4. If gate passed, merge auto_approved into golden master
        if dq_report.gate_passed:
            merged_count = self.storage.merge_master(run_id)
            logger.info("Merged %d records into golden master.", merged_count)
        else:
            logger.warning("Quality Gate FAILED. Run %s gated. Skipping master merge.", run_id)

        # 5. Export deliverables
        self._export_deliverables(run_id, all_results, run_record, dq_report)

        return run_record

    def _export_deliverables(
        self,
        run_id: str,
        results: list[dict[str, Any]],
        run_record: dict[str, Any],
        dq_report: Any,
    ) -> None:
        out_dir = self.settings.resolve(self.settings.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        res_df = pl.DataFrame(results)

        # A. Delivery CSV (original 8 columns + Texas Bar enrichment + 4 audit columns)
        final_csv_df = format_delivery_dataframe(res_df)
        csv_path = out_dir / "enriched_tx_lawyers.csv"
        final_csv_df.write_csv(csv_path)
        logger.info("Exported delivery CSV to %s", csv_path)

        # B. Delivery NDJSON (BigQuery native line-delimited format)
        ndjson_path = out_dir / "enriched_tx_lawyers.ndjson"
        with ndjson_path.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, default=str) + "\n")
        logger.info("Exported delivery NDJSON to %s", ndjson_path)

        # C. Run Report Summary (JSON)
        report_path = out_dir / "run_report.json"
        summary_payload = {
            "run_id": run_id,
            "timestamp": run_record["finished_at"].isoformat()
            if isinstance(run_record["finished_at"], datetime)
            else str(run_record["finished_at"]),
            "metrics": {
                "total": run_record["total"],
                "auto_approved": run_record["auto_approved"],
                "auto_approved_pct": f"{(run_record['auto_approved'] / max(1, run_record['total'])) * 100:.1f}%",
                "pending_review": run_record["pending_review"],
                "not_found": run_record["not_found"],
                "firm_changes_detected": run_record["firm_changes"],
                "http_requests": run_record["http_requests"],
            },
            "quality_gate": {
                "passed": dq_report.gate_passed,
                "violations": dq_report.gate_violations,
                "missing_phone_ratio": f"{dq_report.missing_phone_ratio:.1%}",
                "not_found_ratio": f"{dq_report.not_found_ratio:.1%}",
                "rule_issues_summary": dq_report.rule_summary,
            },
        }
        report_path.write_text(json.dumps(summary_payload, indent=2))
        logger.info("Exported run quality report to %s", report_path)
