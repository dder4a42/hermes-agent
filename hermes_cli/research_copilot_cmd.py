"""CLI handlers for ``hermes research-copilot``."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _remove_config_entry(*, path: Path, key: str, entry_id: str) -> Path | None:
    """Remove one id-addressed YAML list entry, preserving a timestamped backup."""
    from datetime import datetime, timezone
    import shutil

    from research_copilot.runtime import load_yaml
    from utils import atomic_yaml_write

    data = load_yaml(path)
    entries = data.get(key)
    if not isinstance(entries, list):
        raise ValueError(f"{key} must be a list: {path}")
    retained = [entry for entry in entries if not (isinstance(entry, dict) and str(entry.get("id")) == entry_id)]
    if len(retained) == len(entries):
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = path.parent / "backups" / f"preferences-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / path.name
    shutil.copy2(path, backup)
    data[key] = retained
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_yaml_write(path, data, sort_keys=False)
    return backup


def _runtime():
    from research_copilot.runtime import runtime_paths

    return runtime_paths()


def _load_catalog_and_topics():
    from research_copilot.preferences import load_research_preferences
    from research_copilot.runtime import provider_registry
    from research_copilot.sources import load_source_catalog

    paths = _runtime()
    registry = provider_registry()
    preferences = load_research_preferences(paths["topics"], paths["research_config"])
    topics = [
        {
            "id": topic.id,
            "name": topic.name,
            "status": topic.status,
            "description": topic.description,
            "search_queries": list(topic.search_queries),
            "match_terms": list(topic.match_terms),
            "exclude_terms": list(topic.exclude_terms),
        }
        for topic in preferences.active_topics
    ]
    catalog = load_source_catalog(
        paths["catalog"], provider_ids=registry.ids(),
        topic_ids={topic.id for topic in preferences.topics},
        option_specs=registry.option_specs(),
    )
    return paths, registry, topics, catalog, preferences


def _sources(args: Any) -> int:
    command = getattr(args, "research_sources_command", None)
    if command in {"enable", "disable", "set-option", "set-budget"}:
        from research_copilot.runtime import load_yaml
        from utils import atomic_yaml_write

        path = _runtime()["catalog"]
        source_id = str(args.source_id)
        try:
            document = load_yaml(path)
            sources = document.get("sources")
            if not isinstance(sources, list):
                raise ValueError(f"sources must be a list: {path}")
            match = next(
                (
                    source for source in sources
                    if isinstance(source, dict) and str(source.get("id")) == source_id
                ),
                None,
            )
            if match is None:
                raise ValueError(f"Source not found: {source_id}")
            if command == "set-option":
                import yaml
                from research_copilot.runtime import provider_registry

                options = match.setdefault("options", {})
                if not isinstance(options, dict):
                    raise ValueError(f"Source options must be a mapping: {source_id}")
                provider_id = str(match.get("provider") or "")
                option_spec = provider_registry().option_specs().get(provider_id)
                if option_spec is None or str(args.key) not in option_spec.allowed:
                    raise ValueError(
                        f"Unsupported option {args.key!r} for provider {provider_id!r}"
                    )
                options[str(args.key)] = yaml.safe_load(str(args.value))
            elif command == "set-budget":
                if int(args.value) < 0:
                    raise ValueError("Source budget values cannot be negative")
                budget = match.setdefault("budget", {})
                if not isinstance(budget, dict):
                    raise ValueError(f"Source budget must be a mapping: {source_id}")
                budget[str(args.key)] = int(args.value)
            else:
                match["enabled"] = command == "enable"
            # Source Catalog v1 intentionally has no mutable root metadata.
            # Also clean files written by the short-lived pre-fix toggle CLI.
            document.pop("updated_at", None)
            atomic_yaml_write(path, document, sort_keys=False)
        except Exception as exc:
            print(f"Failed to {command} source: {exc}")
            return 1
        if command == "set-option":
            print(f"Source {source_id}: option {args.key} updated")
        elif command == "set-budget":
            print(f"Source {source_id}: budget {args.key}={args.value}")
        else:
            print(f"Source {source_id}: {'enabled' if match['enabled'] else 'disabled'}")
        return 0
    try:
        paths, _registry, _topics, catalog, preferences = _load_catalog_and_topics()
    except Exception as exc:
        print(f"Source Catalog invalid: {exc}")
        return 1
    if command == "validate":
        print(f"Source Catalog valid: {paths['catalog']} ({len(catalog.sources)} sources)")
        for warning in preferences.warnings:
            print(f"Warning: {warning}")
        for warning in catalog.warnings:
            print(f"Warning: {warning}")
        return 0
    for source in catalog.sources:
        state = "enabled" if source.enabled else "disabled"
        print(f"{source.id}\t{state}\t{source.provider}\ttier={source.tier:.2f}")
    return 0


def _topics(args: Any) -> int:
    from research_copilot.preferences import load_research_preferences

    path = _runtime()["topics"]
    command = getattr(args, "research_topics_command", None)
    if command == "remove":
        topic_id = str(args.topic_id)
        try:
            backup = _remove_config_entry(path=path, key="topics", entry_id=topic_id)
        except Exception as exc:
            print(f"Failed to remove research topic: {exc}")
            return 1
        if backup is None:
            print(f"Research topic not found: {topic_id}")
            return 1
        print(f"Removed research topic: {topic_id}")
        print(f"Backup: {backup}")
        return 0
    try:
        preferences = load_research_preferences(path, _runtime()["research_config"])
    except Exception as exc:
        print(f"Failed to load research topics: {exc}")
        return 1
    for topic in preferences.topics:
        priority, _questions = preferences.ranking_values(topic.id)
        print(f"{topic.id}\t{topic.status}\tpriority={priority}\t{topic.name}")
    return 0


def _profile(args: Any) -> int:
    from research_copilot.runtime import load_yaml

    path = _runtime()["research_config"]
    command = getattr(args, "research_profile_command", None)
    if command == "remove-agenda":
        agenda_id = str(args.agenda_id)
        try:
            backup = _remove_config_entry(path=path, key="long_term_agenda", entry_id=agenda_id)
        except Exception as exc:
            print(f"Failed to remove research agenda: {exc}")
            return 1
        if backup is None:
            print(f"Research agenda not found: {agenda_id}")
            return 1
        print(f"Removed research agenda: {agenda_id}")
        print(f"Backup: {backup}")
        return 0
    try:
        entries = load_yaml(path).get("long_term_agenda", [])
    except Exception as exc:
        print(f"Failed to load research profile: {exc}")
        return 1
    for entry in entries:
        if isinstance(entry, dict):
            print(f"{entry.get('id')}\tpriority={entry.get('priority', 0.5)}\t{entry.get('name', '')}")
    return 0


def _config(args: Any) -> int:
    from research_copilot.preferences import (
        load_research_preferences,
        migrate_research_preferences,
    )

    paths = _runtime()
    command = getattr(args, "research_config_command", None)
    try:
        if command == "migrate":
            report = migrate_research_preferences(
                paths["topics"], paths["research_config"],
                apply=bool(getattr(args, "apply", False)),
            )
            state = "applied" if report.applied else "preview"
            print(f"Research preferences migration ({state}): changed={report.changed}")
            for warning in report.warnings:
                print(f"Warning: {warning}")
            if report.topics_backup:
                print(f"Topics backup: {report.topics_backup}")
                print(f"Profile backup: {report.profile_backup}")
            elif report.changed:
                print("Run with --apply to write schema v2 after backup.")
            return 0
        preferences = load_research_preferences(paths["topics"], paths["research_config"])
    except Exception as exc:
        print(f"Research preferences invalid: {exc}")
        return 1
    print(
        "Research preferences valid: "
        f"topics_schema={preferences.topics_schema_version} "
        f"profile_schema={preferences.profile_schema_version} "
        f"topics={len(preferences.topics)} agenda={len(preferences.agenda)}"
    )
    for warning in preferences.warnings:
        print(f"Warning: {warning}")
    return 0


def _collect(args: Any) -> int:
    import hashlib
    from datetime import datetime, timezone
    from research_copilot.runtime import collection_config, open_library
    from research_copilot.sources import CollectionBudget, FailureCooldownPolicy, SourceRunner

    try:
        paths, registry, topics, catalog, _preferences = _load_catalog_and_topics()
        connection, repository = open_library(paths["database"])
        try:
            requested_max_requests = getattr(args, "max_requests", None)
            requested_max_items = getattr(args, "max_new_items", None)
            max_requests = 80 if requested_max_requests is None else int(requested_max_requests)
            max_new_items = 120 if requested_max_items is None else int(requested_max_items)
            if max_requests < 1 or max_new_items < 1:
                raise ValueError("collection run limits must be at least 1")
            topic_ids = tuple(str(topic["id"]) for topic in topics)
            queries = {
                str(topic["id"]): tuple(str(v) for v in topic.get("search_queries", []))
                for topic in topics
            }
            match_terms = {
                str(topic["id"]): tuple(str(v) for v in topic.get("match_terms", []))
                for topic in topics
            }
            excludes = {
                str(topic["id"]): tuple(str(v) for v in topic.get("exclude_terms", []))
                for topic in topics
            }
            resilience = collection_config()
            summary = SourceRunner(
                providers=registry, repository=repository,
                budget=CollectionBudget(
                    max_requests=max_requests, max_new_items=max_new_items,
                ),
                cooldown_policy=FailureCooldownPolicy(
                    threshold=resilience["failure_threshold"],
                    base_seconds=resilience["failure_base_seconds"],
                    max_seconds=resilience["failure_max_seconds"],
                ),
            ).collect(
                catalog, active_topic_ids=topic_ids, topic_queries=queries,
                topic_match_terms=match_terms,
                topic_excludes=excludes, started_at=datetime.now(timezone.utc),
                dry_run=bool(args.dry_run), only_source_id=args.source,
                run_metadata={
                    "topics_revision": hashlib.sha256(paths["topics"].read_bytes()).hexdigest(),
                    "profile_revision": hashlib.sha256(paths["research_config"].read_bytes()).hexdigest(),
                    "catalog_revision": hashlib.sha256(paths["catalog"].read_bytes()).hexdigest(),
                    "topics_schema_version": _preferences.topics_schema_version,
                    "profile_schema_version": _preferences.profile_schema_version,
                },
            )
        finally:
            connection.close()
    except Exception as exc:
        print(f"Research collection failed: {exc}")
        return 1
    mode = "dry-run" if summary.dry_run else "persisted"
    print(f"Research collection ({mode}): requests={summary.requests}, new={summary.new}")
    for source in summary.sources:
        cooldown = f", until={source.cooldown_until}" if source.cooldown_until else ""
        print(
            f"  {source.source_id}: {source.status}; fetched={source.fetched}, "
            f"new={source.new}, merged={source.merged}, unchanged={source.unchanged}, "
            f"filtered={source.filtered}{cooldown}"
        )
        if source.error_code or source.error_message:
            print(
                f"    error={source.error_code or 'provider_error'}: "
                f"{source.error_message or 'no detail'}"
            )
        if summary.dry_run and bool(getattr(args, "show_items", False)):
            if source.metrics:
                import json

                print(
                    "    metrics="
                    + json.dumps(source.metrics, ensure_ascii=False, sort_keys=True)
                )
            for item in source.previews:
                topics = ",".join(item.topic_ids) or "none"
                print(
                    f"    [{item.disposition}{':' + item.reason if item.reason else ''}] "
                    f"{item.title} | "
                    f"topics={topics} | {item.url}"
                )
    return 0 if all(source.status != "failed" for source in summary.sources) else 1


def _health(_args: Any) -> int:
    from research_copilot.health import build_health_report, render_health_report
    from research_copilot.runtime import open_library

    paths = _runtime()
    connection, _repository = open_library(paths["database"])
    try:
        print(render_health_report(build_health_report(connection)))
    finally:
        connection.close()
    return 0


def _scout(args: Any) -> int:
    from datetime import datetime, timezone
    from research_copilot.scout import (
        newsletter_news_highlights, render_scout_report, run_hermes_scout,
        save_scout_result,
    )
    from research_copilot.runtime import open_library, scout_config

    paths = _runtime()
    if getattr(args, "promote", None):
        from research_copilot.preferences import load_research_preferences
        from research_copilot.scout import promote_scout_candidates

        try:
            preferences = load_research_preferences(
                paths["topics"], paths["research_config"],
            )
            connection, repository = open_library(paths["database"])
            try:
                promoted = promote_scout_candidates(
                    Path(args.promote).expanduser(), repository=repository,
                    allowed_topic_ids={topic.id for topic in preferences.active_topics},
                    candidate_indices=tuple(getattr(args, "candidate", None) or ()),
                )
            finally:
                connection.close()
        except Exception as exc:
            print(f"Scout promotion failed: {exc}")
            return 1
        for item in promoted:
            print(
                f"promoted candidate {item['index']}: {item['item_id']} "
                f"({item['disposition']}) — {item['title']}"
            )
        return 0
    try:
        config = scout_config()
        requested_timeout = getattr(args, "timeout", None)
        result = run_hermes_scout(
            paths=paths,
            timeout_seconds=max(60, int(requested_timeout or config["timeout_seconds"])),
            profile=str(config["execution_profile"]),
        )
    except Exception as exc:
        print(f"Research scout failed: {exc}")
        return 1
    highlights = []
    # 本周订阅邮件里的新闻热点（主管道的 topic 过滤会丢弃它们，周报里补回来）
    try:
        connection, _repository = open_library(paths["database"])
        try:
            highlights = newsletter_news_highlights(connection, days=7)
        finally:
            connection.close()
    except Exception as exc:
        highlights = [{"title": f"新闻热点读取失败：{exc}", "url": ""}]
    report = render_scout_report(
        result.payload, newsletter_highlights=highlights,
    )
    if not bool(getattr(args, "dry_run", False)):
        destination = save_scout_result(paths["data"], result.payload)
        if bool(getattr(args, "delivery_context", False)):
            connection, repository = open_library(paths["database"])
            try:
                outbox_id = repository.enqueue_scout_delivery(
                    artifact_path=str(destination), payload=report,
                    created_at=datetime.now(timezone.utc),
                )
                repository.mark_scout_delivery_attempt(
                    outbox_id, attempted_at=datetime.now(timezone.utc),
                )
            finally:
                connection.close()
        else:
            report += f"已暂存：{destination}\n"
    print(report, end="")
    return 0


def _doctor(_args: Any) -> int:
    from hermes_cli import __version__
    from research_copilot.health import build_doctor_report, render_doctor_report
    from research_copilot.runtime import open_library

    paths = _runtime()
    connection, _repository = open_library(paths["database"])
    try:
        report = build_doctor_report(
            connection, database_path=paths["database"], catalog_path=paths["catalog"],
            topics_path=paths["topics"], profile_path=paths["research_config"],
            project_root=Path(__file__).resolve().parent.parent,
            hermes_home=paths["home"], hermes_version=__version__,
        )
        print(render_doctor_report(report))
    finally:
        connection.close()
    return 0


def _duplicates(args: Any) -> int:
    from datetime import datetime, timezone

    from research_copilot.runtime import open_library

    paths = _runtime()
    connection, repository = open_library(paths["database"])
    try:
        source_id = str(getattr(args, "merge", None) or "").strip()
        target_id = str(getattr(args, "into", None) or "").strip()
        if source_id or target_id:
            if not source_id or not target_id:
                print("Duplicate merge requires both --merge and --into.")
                return 1
            if not bool(getattr(args, "apply", False)):
                print(
                    f"Duplicate merge preview: {source_id} -> {target_id}\n"
                    "Re-run with --apply and a non-empty --reason after verification."
                )
                return 0
            try:
                repository.merge_items(
                    source_item_id=source_id, target_item_id=target_id,
                    observed_at=datetime.now(timezone.utc),
                    reason=str(getattr(args, "reason", "")),
                )
            except (KeyError, ValueError) as exc:
                print(f"Duplicate merge refused: {exc}")
                return 1
            print(f"Duplicate merged: {source_id} -> {target_id}")
            return 0

        rows = connection.execute(
            """SELECT normalized_title,count(*) AS count,group_concat(id) AS ids
               FROM research_items GROUP BY normalized_title HAVING count(*) > 1
               ORDER BY normalized_title"""
        ).fetchall()
        print(f"Duplicate normalized-title groups: {len(rows)}")
        for row in rows:
            print(f"- {row['normalized_title']}: {row['ids']}")
        return 0
    finally:
        connection.close()


def _wiki(args: Any) -> int:
    command = getattr(args, "research_wiki_command", None)
    if command == "configure":
        from hermes_cli.config import load_config, save_config

        vault = Path(str(args.vault)).expanduser()
        subdir = str(args.library_subdir or "Research Library").strip()
        if not subdir or Path(subdir).is_absolute() or ".." in Path(subdir).parts:
            print("Wiki library subdirectory must be a safe relative directory.")
            return 1
        config = load_config()
        research = config.setdefault("research_copilot", {})
        if not isinstance(research, dict):
            print("research_copilot in config.yaml must be a mapping.")
            return 1
        research["wiki"] = {
            "vault_path": str(vault),
            "library_subdir": subdir,
        }
        save_config(config)
        print("Research Wiki configured in config.yaml")
        print(f"  Vault:   {vault}")
        print(f"  Library: {vault / subdir}")
        return 0

    from research_copilot.runtime import open_library
    from research_copilot.wiki import audit_wiki, render_audit, resolve_wiki_root

    try:
        root, subdir, vault = resolve_wiki_root(getattr(args, "vault", None))
    except Exception as exc:
        print(f"Research Wiki configuration invalid: {exc}")
        return 1
    if command == "export":
        from research_copilot.export_obsidian import export

        paths = _runtime()
        try:
            connection, _repository = open_library(paths["database"])
            connection.close()
            stats = export(
                vault, paths["database"], paths["reports"],
                full=bool(getattr(args, "full", False)), library_subdir=subdir,
            )
        except Exception as exc:
            print(f"Research Wiki export failed: {exc}")
            return 1
        print(
            "Research Wiki exported: "
            f"papers={stats['papers']} news={stats['news']} "
            f"created={stats['created']} updated={stats['updated']} "
            f"merged_records={stats['merged_records']} "
            f"retired_duplicates={stats['retired_duplicates']} "
            f"reports_copied={stats['reports_archived']}"
        )
        print(f"  {root}")
        return 0
    if command == "lint":
        try:
            audit = audit_wiki(root)
        except Exception as exc:
            print(f"Research Wiki lint failed: {exc}")
            return 1
        print(render_audit(audit, as_json=bool(getattr(args, "json", False))))
        return 1 if audit.errors else 0
    if command == "publish-check":
        from research_copilot.vault_sync import check_vault_publication, render_publish_check

        result = check_vault_publication()
        print(render_publish_check(result))
        return 0 if result.allowed else 1
    if command == "reconcile":
        from datetime import datetime, timezone
        from research_copilot.wiki import reconcile_wiki_analysis

        paths = _runtime()
        connection, repository = open_library(paths["database"])
        try:
            report = reconcile_wiki_analysis(
                root, repository, apply=bool(getattr(args, "apply", False)),
                updated_at=datetime.now(timezone.utc),
            )
        finally:
            connection.close()
        mode = "applied" if bool(getattr(args, "apply", False)) else "preview"
        print(
            f"Research Wiki reconciliation ({mode}): scanned={report.scanned} "
            f"analyzed={report.analyzed_pages} matched={report.matched_items} "
            f"candidates={len(report.candidates)} updated={len(report.updated)} "
            f"unmatched={len(report.unmatched)}"
        )
        for path in report.unmatched:
            print(f"  unmatched: {path}")
        return 0
    print("Usage: hermes research wiki <configure|export|lint|publish-check|reconcile>")
    return 1


def _ranking_policies(topics, preferences):
    from research_copilot.ranking import AgendaPolicy, PromptPolicy, TopicPolicy

    topic_policies = {
        str(topic["id"]): TopicPolicy(
            id=str(topic["id"]), priority=preferences.ranking_values(str(topic["id"]))[0],
            include=tuple(str(value) for value in topic.get("match_terms", [])),
            open_questions=preferences.ranking_values(str(topic["id"]))[1],
        ) for topic in topics
    }
    agenda_policies = {
        agenda.id: AgendaPolicy(
            id=agenda.id, priority=agenda.priority, topic_ids=agenda.topic_ids,
            open_questions=tuple(PromptPolicy(value.id, value.text) for value in agenda.open_question_refs),
            knowledge_gaps=tuple(PromptPolicy(value.id, value.text) for value in agenda.knowledge_gap_refs),
        ) for agenda in preferences.agenda
    }
    return topic_policies, agenda_policies


def _recommendation_delivery_payload(*, item, winner, topic_matches, profile, preferences):
    from research_copilot.learning import build_learning_package

    prompt_texts = {
        prompt.id: prompt.text
        for agenda in preferences.agenda
        for prompt in (*agenda.open_question_refs, *agenda.knowledge_gap_refs)
    }
    learning_package = build_learning_package(
        item or {"id": winner.item_id}, winner, prompt_texts=prompt_texts,
    )
    return {
        "kind": "research_recommendation_context",
        "item": item or {"id": winner.item_id},
        "ranking": {
            "score": winner.score, "dimensions": dict(winner.dimensions),
            "reasons": list(winner.reasons),
            "primary_topic_id": winner.primary_topic_id,
            "secondary_topic_ids": list(winner.secondary_topic_ids),
            "matched_agenda_ids": list(winner.matched_agenda_ids),
            "matched_question_ids": list(winner.matched_question_ids),
            "matched_knowledge_gap_ids": list(winner.matched_knowledge_gap_ids),
            "suggested_action": winner.suggested_action,
        },
        "matched_topics": topic_matches,
        "learning_package": learning_package.to_dict(),
        "research_profile": {
            "positioning": str(profile.get("positioning") or ""),
            "long_term_agenda": profile.get("long_term_agenda") or [],
        },
    }


def _score_from_pending_delivery(pending):
    import json
    from research_copilot.ranking import ScoreResult

    reasons = tuple(
        part.strip() for part in str(pending.get("rationale") or "").split(" · ")
        if part.strip()
    )

    def value(prefix: str) -> str | None:
        return next(
            (part.split("=", 1)[1] for part in reasons if part.startswith(prefix + "=")),
            None,
        )

    def values(prefix: str) -> tuple[str, ...]:
        return tuple(part for part in (value(prefix) or "").split(",") if part)

    return ScoreResult(
        item_id=str(pending["item_id"]), score=float(pending["score"]),
        dimensions=json.loads(pending.get("score_breakdown_json") or "{}"),
        reasons=reasons, primary_topic_id=value("primary_topic"),
        secondary_topic_ids=values("secondary_topics"),
        matched_agenda_ids=values("agenda"),
        matched_question_ids=values("questions"),
        matched_knowledge_gap_ids=values("knowledge_gaps"),
        suggested_action=value("suggested_action") or "review",
    )


def _recommend(args: Any) -> int:
    import json
    from datetime import datetime, timezone
    from research_copilot.ranking import RankingService, RecommendationBudget
    from research_copilot.runtime import load_yaml, open_library, ranking_config

    delivery_context = bool(getattr(args, "delivery_context", False))
    delivery_payload = None
    winner = None
    try:
        paths, _registry, topics, _catalog, preferences = _load_catalog_and_topics()
        policies, agendas = _ranking_policies(topics, preferences)
        config = ranking_config()
        budget = RecommendationBudget(
            daily_limit=config["daily_recommendation_limit"],
            weekly_limit=config["weekly_recommendation_limit"],
        )
        try:
            profile = load_yaml(paths["research_config"])
        except (FileNotFoundError, ValueError):
            profile = {}
        connection, repository = open_library(paths["database"])
        try:
            service = RankingService(repository)
            now = datetime.now(timezone.utc)
            budget_status = service.budget_status(at=now, budget=budget)
            pending = (
                repository.pending_delivery()
                if delivery_context and not args.dry_run else None
            )
            if pending is not None and pending.get("payload"):
                repository.mark_delivery_attempt(pending["id"], attempted_at=now)
                delivery_payload = pending["payload"]
            else:
                if pending is not None:
                    winner = _score_from_pending_delivery(pending)
                elif args.dry_run:
                    results = service.rank(
                        topics=policies, agendas=agendas, threshold=args.threshold,
                        limit=1, now=now,
                        secondary_topic_bonus_cap=config["secondary_topic_bonus_cap"],
                    )
                    winner = results[0] if results else None
                else:
                    winner = service.recommend_top(
                        topics=policies, agendas=agendas, recommended_at=now,
                        threshold=args.threshold, budget=budget,
                        secondary_topic_bonus_cap=config["secondary_topic_bonus_cap"],
                        enqueue_delivery=delivery_context,
                    )

                if winner is not None:
                    row = connection.execute(
                        """SELECT id,title,summary,url,item_type,published_at,
                                  agent_analysis_status,user_learning_status
                           FROM research_items WHERE id=?""",
                        (winner.item_id,),
                    ).fetchone()
                    item = dict(row) if row is not None else None
                    topic_matches = []
                    configured_topics = {str(topic.get("id")): topic for topic in topics}
                    for matched in connection.execute(
                        "SELECT topic_id,confidence FROM item_topics WHERE item_id=? "
                        "ORDER BY confidence DESC,topic_id", (winner.item_id,),
                    ):
                        configured = configured_topics.get(str(matched["topic_id"]), {})
                        topic_matches.append({
                            "id": str(matched["topic_id"]),
                            "name": str(configured.get("name") or matched["topic_id"]),
                            "confidence": float(matched["confidence"]),
                            "priority": preferences.ranking_values(str(matched["topic_id"]))[0],
                            "description": str(configured.get("description") or ""),
                            "open_questions": list(preferences.ranking_values(str(matched["topic_id"]))[1]),
                        })
                    if delivery_context:
                        delivery_payload = _recommendation_delivery_payload(
                            item=item, winner=winner, topic_matches=topic_matches,
                            profile=profile, preferences=preferences,
                        )
                        if not args.dry_run:
                            pending = repository.pending_delivery()
                            if pending is None or pending["item_id"] != winner.item_id:
                                raise RuntimeError("Recommendation delivery outbox is inconsistent")
                            repository.set_delivery_payload(
                                pending["recommendation_id"], payload=delivery_payload,
                            )
                            repository.mark_delivery_attempt(
                                pending["id"], attempted_at=now,
                            )
        finally:
            connection.close()
    except Exception as exc:
        print(f"Recommendation failed: {exc}")
        return 1
    if delivery_payload is not None:
        print(json.dumps(delivery_payload, ensure_ascii=False, sort_keys=True))
        return 0
    if winner is None:
        if delivery_context:
            return 0
        if not args.dry_run and not budget_status.allowed:
            print(
                "Recommendation budget exhausted: "
                f"daily={budget_status.daily_used}/{budget_status.daily_limit}, "
                f"weekly={budget_status.weekly_used}/{budget_status.weekly_limit}."
            )
            return 0
        print("No Research Item cleared the recommendation threshold.")
        return 0
    mode = "dry-run" if args.dry_run else "recommended"
    print(f"{mode}: {winner.item_id} score={winner.score:.4f}")
    print("  " + " · ".join(winner.reasons))
    return 0


def _triage(args: Any) -> int:
    import json
    from dataclasses import asdict
    from datetime import datetime, timezone
    from research_copilot.ranking import RankingService
    from research_copilot.runtime import open_library, ranking_config

    try:
        paths, _registry, topics, _catalog, preferences = _load_catalog_and_topics()
        policies, agendas = _ranking_policies(topics, preferences)
        config = ranking_config()
        limit = config["daily_triage_limit"] if args.limit is None else max(0, int(args.limit))
        connection, repository = open_library(paths["database"])
        try:
            cards = RankingService(repository).triage(
                topics=policies, agendas=agendas, threshold=float(args.threshold), limit=limit,
                max_chars=config["triage_card_max_chars"],
                secondary_topic_bonus_cap=config["secondary_topic_bonus_cap"],
                now=datetime.now(timezone.utc),
            )
        finally:
            connection.close()
    except Exception as exc:
        print(f"Research triage failed: {exc}")
        return 1
    if bool(args.json):
        print(json.dumps([asdict(card) for card in cards], ensure_ascii=False, sort_keys=True))
        return 0
    print(f"Research triage queue ({len(cards)}/{limit})")
    for index, card in enumerate(cards, start=1):
        result = card.ranking
        print(f"\n{index}. {card.title} [{card.item_id}] score={result.score:.4f}")
        if card.excerpt:
            print(f"   {card.excerpt}")
        print(
            f"   primary={result.primary_topic_id or '-'}; "
            f"agenda={','.join(result.matched_agenda_ids) or '-'}; "
            f"questions={','.join(result.matched_question_ids) or '-'}; "
            f"gaps={','.join(result.matched_knowledge_gap_ids) or '-'}; "
            f"action={result.suggested_action}"
        )
    return 0


def _deep_research(args: Any) -> int:
    from datetime import datetime, timezone

    from research_copilot.deep_research import (
        load_deep_research_artifact,
        render_deep_research_learning_card,
        render_deep_research_markdown,
        run_hermes_deep_research,
        select_deep_research_target,
    )
    from research_copilot.runtime import deep_research_config, open_library

    command = getattr(args, "research_deep_research_command", None)
    if command not in {"import", "run"}:
        print("Usage: hermes research deep-research <import|run>")
        return 1
    try:
        paths = _runtime()
        delivery_context = command == "run" and bool(
            getattr(args, "delivery_context", False)
        )
        if delivery_context:
            connection, repository = open_library(paths["database"])
            try:
                pending_delivery = repository.pending_deep_research_delivery()
                if pending_delivery is not None:
                    repository.mark_deep_research_delivery_attempt(
                        pending_delivery["id"], attempted_at=datetime.now(timezone.utc),
                    )
            finally:
                connection.close()
            if pending_delivery is not None:
                print(pending_delivery["payload_text"], end="")
                return 0
        if command == "run":
            connection, _repository = open_library(paths["database"])
            try:
                target = select_deep_research_target(
                    connection, item_id=getattr(args, "item_id", None),
                )
            finally:
                connection.close()
            if bool(getattr(args, "dry_run", False)):
                print(
                    "Deep-research dry run\n"
                    f"  Item: {target.item_id} — {target.title}\n"
                    f"  URL: {target.url or '-'}\n"
                    f"  Topics: {', '.join(target.topic_ids) or '-'}\n"
                    "  Boundary: one research-copilot process; web toolset only; no subagents or file writes"
                )
                return 0
            settings = deep_research_config()
            timeout = settings["timeout_seconds"] if args.timeout is None else int(args.timeout)
            if timeout < 60:
                raise ValueError("deep-research timeout must be at least 60 seconds")
            artifact = run_hermes_deep_research(
                target=target, data_dir=paths["data"],
                research_config_path=paths["research_config"],
                profile=settings["execution_profile"], timeout_seconds=timeout,
            )
            should_apply = True
        else:
            artifact = load_deep_research_artifact(args.path)
            should_apply = bool(getattr(args, "apply", False))

        connection, repository = open_library(paths["database"])
        try:
            item = connection.execute(
                "SELECT title,url FROM research_items WHERE id=?", (artifact.item_id,),
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown Research Item: {artifact.item_id}")
            created = False
            artifact_id = artifact.artifact_id
            delivery_payload = (
                render_deep_research_learning_card(
                    artifact, title=str(item["title"]), url=str(item["url"] or ""),
                )
                if delivery_context else None
            )
            if should_apply:
                artifact_id, created = repository.import_deep_research_artifact(
                    artifact_id=artifact.artifact_id, item_id=artifact.item_id,
                    schema_version=1, generated_at=artifact.generated_at,
                    imported_at=datetime.now(timezone.utc),
                    research_question=artifact.research_question,
                    content_hash=artifact.content_hash, artifact=artifact.document,
                    producer=artifact.producer,
                    delivery_payload=delivery_payload,
                )
                if delivery_context:
                    pending_delivery = repository.pending_deep_research_delivery()
                    if pending_delivery is None or pending_delivery["artifact_id"] != artifact_id:
                        raise RuntimeError("Deep-research delivery outbox is inconsistent")
                    repository.mark_deep_research_delivery_attempt(
                        pending_delivery["id"], attempted_at=datetime.now(timezone.utc),
                    )
        finally:
            connection.close()

        if command == "run" and not bool(getattr(args, "no_export", False)):
            from research_copilot.export_obsidian import export
            from research_copilot.runtime import wiki_config

            wiki = wiki_config()
            export(
                wiki["vault_path"], paths["database"], reports_dir=paths["reports"],
                full=False, library_subdir=wiki["library_subdir"],
            )
    except Exception as exc:
        print(f"Deep research failed: {exc}")
        return 1
    mode = "imported" if should_apply and created else (
        "already imported" if should_apply else "valid preview"
    )
    if delivery_context:
        print(delivery_payload, end="")
    else:
        print(
            f"Deep-research artifact {mode}: {artifact_id}\n"
            f"  Item: {artifact.item_id} — {item['title']}\n"
            f"  Sources: {len(artifact.sources)}; claims: {len(artifact.claims)}\n"
            f"  Hash: {artifact.content_hash}"
        )
    if command == "run" and not delivery_context:
        print()
        print(render_deep_research_markdown(
            artifact, title=str(item["title"]), url=str(item["url"] or ""),
        ))
    return 0


def _enrich_newsletters(args: Any) -> int:
    from research_copilot.enrichment import NewsletterEnrichmentService
    from research_copilot.runtime import open_library
    paths = _runtime(); connection, _ = open_library(paths["database"])
    try:
        summary = NewsletterEnrichmentService(connection).enrich(
            limit=max(0, int(args.limit)), dry_run=bool(args.dry_run),
        )
    finally:
        connection.close()
    mode = "dry-run" if summary.dry_run else "persisted"
    print(f"Newsletter enrichment ({mode}): selected={summary.selected}, resolved={summary.resolved}, metadata={summary.metadata_fetched}, failed={summary.failed}")
    # Successful entries are committed independently.  Preserve a useful
    # partial batch as a successful cron run; fail only when work was selected
    # but not a single URL could be resolved.
    return 1 if summary.selected > 0 and summary.failed == summary.selected else 0


def _promote_newsletters(args: Any) -> int:
    from datetime import datetime, timezone
    from research_copilot.newsletters import NewsletterPromoter
    from research_copilot.runtime import open_library
    paths, _registry, topics, _catalog, _preferences = _load_catalog_and_topics()
    policies = {
        str(topic["id"]): {
            "include": tuple(str(value) for value in topic.get("match_terms", [])),
            "exclude": tuple(str(value) for value in topic.get("exclude_terms", [])),
        }
        for topic in topics
    }
    connection, _ = open_library(paths["database"])
    try:
        summary = NewsletterPromoter(connection).promote(
            limit=max(0, int(args.limit)), dry_run=bool(args.dry_run),
            now=datetime.now(timezone.utc), topic_policies=policies,
        )
    finally:
        connection.close()
    mode = "dry-run" if summary.dry_run else "persisted"
    print(f"Newsletter promotion ({mode}): selected={summary.selected}, new={summary.new}, merged={summary.merged}, skipped={summary.skipped}")
    return 0


def _daily_report(args: Any) -> int:
    from datetime import datetime, timezone
    from research_copilot.reporting import DailyReportService, render_daily_report
    from research_copilot.runtime import open_library
    paths = _runtime(); connection, _ = open_library(paths["database"])
    try:
        report = DailyReportService(connection).build(
            generated_at=datetime.now(timezone.utc), days=max(1, int(args.days)),
        )
        print(render_daily_report(report))
    finally:
        connection.close()
    return 0


def _evidence(args: Any) -> int:
    from datetime import datetime, timezone
    from research_copilot.evidence import create_evidence
    from research_copilot.preferences import load_research_preferences
    from research_copilot.runtime import open_library

    paths = _runtime()
    try:
        preferences = load_research_preferences(paths["topics"], paths["research_config"])
        connection, repository = open_library(paths["database"])
        try:
            command = getattr(args, "research_evidence_command", None)
            if command in {"list", "ls"}:
                rows = repository.list_evidence(
                    review_status=getattr(args, "status", None),
                )
                print(f"Research evidence ({len(rows)})")
                for row in rows:
                    target = row["belief_id"] or row["prompt_id"]
                    print(
                        f"  {row['id']} [{row['review_status']}] "
                        f"{row['relation']} {target} <- {row['item_id']}: {row['claim']}"
                    )
                return 0
            if command == "add":
                evidence_id = create_evidence(
                    repository, preferences,
                    item_id=args.item_id,
                    belief_id=args.belief,
                    prompt_id=args.prompt,
                    relation=args.relation,
                    claim_type=args.claim_type,
                    strength=float(args.strength),
                    source_quality=args.source_quality,
                    claim=args.claim,
                    rationale=args.rationale,
                    profile_path=paths["research_config"],
                    created_at=datetime.now(timezone.utc),
                )
                print(f"Pending evidence created: {evidence_id}")
                return 0
            if command == "review":
                repository.review_evidence(
                    args.evidence_id,
                    accepted=bool(args.accept),
                    reviewed_at=datetime.now(timezone.utc),
                    note=args.note,
                )
                decision = "accepted" if args.accept else "rejected"
                print(f"Evidence {decision}: {args.evidence_id}")
                return 0
            print("Usage: hermes research evidence <list|add|review>")
            return 1
        finally:
            connection.close()
    except Exception as exc:
        print(f"Research evidence failed: {exc}")
        return 1


def _profile_proposal(args: Any) -> int:
    from research_copilot.evidence import (
        build_profile_update_proposal,
        render_proposal,
        write_profile_update_proposal,
    )
    from research_copilot.preferences import load_research_preferences
    from research_copilot.runtime import open_library

    paths = _runtime()
    try:
        preferences = load_research_preferences(paths["topics"], paths["research_config"])
        connection, repository = open_library(paths["database"])
        try:
            proposal = build_profile_update_proposal(
                repository, preferences, profile_path=paths["research_config"],
            )
        finally:
            connection.close()
        if bool(args.dry_run):
            print(render_proposal(proposal), end="")
        else:
            destination = write_profile_update_proposal(
                paths["reports"] / "profile-proposals", proposal,
            )
            print(f"Profile update proposal written: {destination}")
        return 0
    except Exception as exc:
        print(f"Profile proposal failed: {exc}")
        return 1


def _source_home_from_args(args: Any) -> Path:
    if getattr(args, "source_home", None):
        return Path(args.source_home).expanduser()

    from hermes_cli.profiles import get_profile_dir, normalize_profile_name

    source = normalize_profile_name(getattr(args, "source", None) or "default")
    return get_profile_dir(source)


def _init_profile(args: Any) -> int:
    from hermes_cli.profiles import (
        create_profile,
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from research_copilot.bootstrap import (
        initialize_research_copilot_home,
        install_research_copilot_cron,
    )

    profile = normalize_profile_name(args.profile_name)
    validate_profile_name(profile)
    if profile == "default":
        print("Use the default profile directly; init-profile is for named Weixin/user profiles.")
        return 1

    if not profile_exists(profile):
        if not getattr(args, "create_profile", False):
            print(f"Profile '{profile}' does not exist. Re-run with --create-profile or create it first:")
            print(f"  hermes profile create {profile} --clone")
            print(f"  hermes research-copilot init-profile {profile}")
            return 1
        try:
            create_profile(
                profile,
                clone_config=bool(getattr(args, "clone", False)),
                no_alias=True,
            )
        except Exception as exc:  # noqa: BLE001 - user-facing CLI boundary
            print(f"Failed to create profile '{profile}': {exc}")
            return 1

    profile_home = get_profile_dir(profile)
    source_home = _source_home_from_args(args)
    if not source_home.exists():
        print(f"Source HERMES_HOME does not exist: {source_home}")
        return 1

    try:
        init_result = initialize_research_copilot_home(profile_home, source_home=source_home)
        cron_result = install_research_copilot_cron(
            profile_home,
            deliver=getattr(args, "deliver", "weixin") or "weixin",
        )
    except Exception as exc:  # noqa: BLE001 - user-facing CLI boundary
        print(f"Failed to initialize Research Copilot for profile '{profile}': {exc}")
        return 1

    print(f"Research Copilot initialized for profile '{profile}'.")
    print(f"  HERMES_HOME: {profile_home}")
    scripts = [item.removeprefix("scripts/") for item in init_result.get("copied", []) if item.startswith("scripts/")]
    jobs = cron_result.get("created", []) + cron_result.get("existing", [])
    print(f"  Data dir:    {init_result['data_dir']}")
    print(f"  Scripts:     {', '.join(scripts) if scripts else 'none copied'}")
    print(f"  Cron jobs:   {', '.join(jobs)}")
    print()
    print("Next steps:")
    print(f"  1. Configure Weixin credentials in {profile_home}/.env")
    print(f"  2. Start or restart the profile gateway: hermes -p {profile} gateway restart")
    print(f"  3. In Weixin, try: /paper topics")
    return 0


def cmd_research_copilot(args: Any) -> int:
    """Dispatch ``hermes research-copilot`` subcommands."""
    command = getattr(args, "research_copilot_command", None)
    if command == "init-profile":
        return _init_profile(args)
    if command == "sources":
        return _sources(args)
    if command == "topics":
        return _topics(args)
    if command == "profile":
        return _profile(args)
    if command == "config":
        return _config(args)
    if command == "collect":
        return _collect(args)
    if command == "scout":
        return _scout(args)
    if command == "health":
        return _health(args)
    if command == "doctor":
        return _doctor(args)
    if command == "duplicates":
        return _duplicates(args)
    if command == "wiki":
        return _wiki(args)
    if command == "recommend":
        return _recommend(args)
    if command == "triage":
        return _triage(args)
    if command == "enrich-newsletters":
        return _enrich_newsletters(args)
    if command == "promote-newsletters":
        return _promote_newsletters(args)
    if command == "daily-report":
        return _daily_report(args)
    if command == "deep-research":
        return _deep_research(args)
    if command == "evidence":
        return _evidence(args)
    if command == "profile-proposal":
        return _profile_proposal(args)
    if command == "migrate-to-library":
        from research_copilot.migration import apply_migration, build_migration_plan, render_migration_plan

        if getattr(args, "apply", False):
            paths = _runtime()
            result = apply_migration(paths["data"], profile_home=paths["home"])
            print("Research Library migration complete")
            print(f"  Backup:          {result.backup_dir}")
            print(f"  Database:        {result.database}")
            print(f"  Imported items:  {result.imported_items}")
            print(f"  Recommendations: {result.imported_recommendations}")
            print(f"  Interactions:    {result.imported_interactions}")
            print(f"  Mapping report:  {result.mapping_report}")
            return 0
        paths = _runtime()
        print(render_migration_plan(build_migration_plan(paths["data"])))
        return 0
    print("Usage: hermes research <config|topics|profile|sources|collect|triage|recommend|health|doctor|wiki>")
    return 1
