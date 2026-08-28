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
    from research_copilot.runtime import active_topics, load_yaml, provider_registry
    from research_copilot.sources import load_source_catalog

    paths = _runtime()
    registry = provider_registry()
    topics = active_topics(paths["topics"])
    catalog = load_source_catalog(
        paths["catalog"], provider_ids=registry.ids(),
        topic_ids={str(topic.get("id")) for topic in topics},
        option_specs=registry.option_specs(),
    )
    return paths, registry, topics, catalog


def _sources(args: Any) -> int:
    try:
        paths, _registry, _topics, catalog = _load_catalog_and_topics()
    except Exception as exc:
        print(f"Source Catalog invalid: {exc}")
        return 1
    if getattr(args, "research_sources_command", None) == "validate":
        print(f"Source Catalog valid: {paths['catalog']} ({len(catalog.sources)} sources)")
        return 0
    for source in catalog.sources:
        state = "enabled" if source.enabled else "disabled"
        print(f"{source.id}\t{state}\t{source.provider}\ttier={source.tier:.2f}")
    return 0


def _topics(args: Any) -> int:
    from research_copilot.runtime import load_yaml

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
        entries = load_yaml(path).get("topics", [])
    except Exception as exc:
        print(f"Failed to load research topics: {exc}")
        return 1
    for entry in entries:
        if isinstance(entry, dict):
            print(f"{entry.get('id')}\t{entry.get('status', 'active')}\tpriority={entry.get('priority', 0.5)}\t{entry.get('name', '')}")
    return 0


def _profile(args: Any) -> int:
    from research_copilot.runtime import load_yaml

    path = _runtime()["profile"]
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


def _collect(args: Any) -> int:
    from datetime import datetime, timezone
    from research_copilot.runtime import open_library
    from research_copilot.sources import SourceRunner

    try:
        paths, registry, topics, catalog = _load_catalog_and_topics()
        connection, repository = open_library(paths["database"])
        try:
            topic_ids = tuple(str(topic["id"]) for topic in topics)
            queries = {
                str(topic["id"]): tuple(
                    dict.fromkeys([str(topic.get("name") or "")] + [str(v) for v in topic.get("include", [])])
                )
                for topic in topics
            }
            excludes = {str(topic["id"]): tuple(str(v) for v in topic.get("exclude", [])) for topic in topics}
            summary = SourceRunner(providers=registry, repository=repository).collect(
                catalog, active_topic_ids=topic_ids, topic_queries=queries,
                topic_excludes=excludes, started_at=datetime.now(timezone.utc),
                dry_run=bool(args.dry_run), only_source_id=args.source,
            )
        finally:
            connection.close()
    except Exception as exc:
        print(f"Research collection failed: {exc}")
        return 1
    mode = "dry-run" if summary.dry_run else "persisted"
    print(f"Research collection ({mode}): requests={summary.requests}, new={summary.new}")
    for source in summary.sources:
        print(
            f"  {source.source_id}: {source.status}; fetched={source.fetched}, "
            f"new={source.new}, merged={source.merged}, unchanged={source.unchanged}, filtered={source.filtered}"
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
    from research_copilot.scout import run_codex_scout, save_scout_result

    paths = _runtime()
    try:
        result = run_codex_scout(
            paths=paths, timeout_seconds=max(60, int(getattr(args, "timeout", 2700))),
        )
    except Exception as exc:
        print(f"Research scout failed: {exc}")
        return 1
    print(result.payload["summary"])
    for candidate in result.payload["candidates"]:
        topics = ", ".join(candidate["topic_ids"]) or "未分类"
        print(f"- {candidate['title']} [{topics}；置信度={candidate['confidence']:.2f}]")
        print(f"  {candidate['url']}")
        print(f"  {candidate['why_relevant']}")
    if result.payload["term_suggestions"]:
        print("术语建议：" + ", ".join(result.payload["term_suggestions"]))
    if result.payload["source_suggestions"]:
        print("来源建议：" + ", ".join(result.payload["source_suggestions"]))
    if not bool(getattr(args, "dry_run", False)):
        destination = save_scout_result(paths["data"], result.payload)
        print(f"已暂存：{destination}")
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
            topics_path=paths["topics"], project_root=Path(__file__).resolve().parent.parent,
            hermes_home=paths["home"], hermes_version=__version__,
        )
        print(render_doctor_report(report))
    finally:
        connection.close()
    return 0


def _recommend(args: Any) -> int:
    import json
    from datetime import datetime, timezone
    from research_copilot.ranking import RankingService, TopicPolicy
    from research_copilot.runtime import load_yaml, open_library

    try:
        paths, _registry, topics, _catalog = _load_catalog_and_topics()
        policies = {
            str(topic["id"]): TopicPolicy(
                id=str(topic["id"]), priority=float(topic.get("priority", 0.5)),
                include=tuple(str(v) for v in topic.get("include", [])),
                open_questions=tuple(str(v) for v in topic.get("open_questions", [])),
            ) for topic in topics
        }
        connection, repository = open_library(paths["database"])
        try:
            service = RankingService(repository)
            now = datetime.now(timezone.utc)
            if args.dry_run:
                results = service.rank(topics=policies, threshold=args.threshold, limit=1, now=now)
                winner = results[0] if results else None
            else:
                winner = service.recommend_top(
                    topics=policies, recommended_at=now, threshold=args.threshold,
                )
            item = None
            topic_matches = []
            if winner is not None:
                row = connection.execute(
                    "SELECT id,title,summary,url,item_type,published_at "
                    "FROM research_items WHERE id=?", (winner.item_id,),
                ).fetchone()
                item = dict(row) if row is not None else None
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
                        "priority": float(configured.get("priority", 0.5)),
                        "description": str(configured.get("description") or ""),
                        "open_questions": [str(value) for value in configured.get("open_questions", [])],
                    })
        finally:
            connection.close()
    except Exception as exc:
        print(f"Recommendation failed: {exc}")
        return 1
    if winner is None:
        if bool(getattr(args, "delivery_context", False)):
            return 0
        print("No Research Item cleared the recommendation threshold.")
        return 0
    if bool(getattr(args, "delivery_context", False)):
        try:
            profile = load_yaml(paths["profile"])
        except (FileNotFoundError, ValueError):
            profile = {}
        print(json.dumps({
            "kind": "research_recommendation_context",
            "item": item or {"id": winner.item_id},
            "ranking": {
                "score": winner.score,
                "dimensions": dict(winner.dimensions),
                "reasons": list(winner.reasons),
            },
            "matched_topics": topic_matches,
            "research_profile": {
                "positioning": str(profile.get("positioning") or ""),
                "long_term_agenda": profile.get("long_term_agenda") or [],
            },
        }, ensure_ascii=False, sort_keys=True))
        return 0
    mode = "dry-run" if args.dry_run else "recommended"
    print(f"{mode}: {winner.item_id} score={winner.score:.4f}")
    print("  " + " · ".join(winner.reasons))
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
    return 0 if summary.failed == 0 else 1


def _promote_newsletters(args: Any) -> int:
    from datetime import datetime, timezone
    from research_copilot.newsletters import NewsletterPromoter
    from research_copilot.runtime import open_library
    paths, _registry, topics, _catalog = _load_catalog_and_topics()
    policies = {
        str(topic["id"]): {
            "include": tuple(dict.fromkeys((str(topic.get("name") or ""), *(str(value) for value in topic.get("include", []))))),
            "exclude": tuple(str(value) for value in topic.get("exclude", [])),
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
    if command == "collect":
        return _collect(args)
    if command == "scout":
        return _scout(args)
    if command == "health":
        return _health(args)
    if command == "doctor":
        return _doctor(args)
    if command == "recommend":
        return _recommend(args)
    if command == "enrich-newsletters":
        return _enrich_newsletters(args)
    if command == "promote-newsletters":
        return _promote_newsletters(args)
    if command == "daily-report":
        return _daily_report(args)
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
    print("Usage: hermes research <topics|profile|sources|collect|health|doctor|recommend>")
    return 1
