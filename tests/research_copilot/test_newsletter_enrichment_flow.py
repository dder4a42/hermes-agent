from datetime import datetime, timezone

from research_copilot.enrichment import NewsletterEnrichmentService, PageMetadata, ResolvedUrl
from research_copilot.library import LibraryRepository, connect_library, initialize_library
from research_copilot.newsletters import NewsletterPromoter


class Resolver:
    def resolve(self,url): return ResolvedUrl(url,"https://example.com/article",(url,"https://example.com/article"),200,"text/html")
class Metadata:
    def fetch(self,url): return PageMetadata(url,"Agent launch","Useful agent memory release",publisher="Example",page_type="NewsArticle",content_hash="abc")


def _staged(tmp_path):
    connection=connect_library(tmp_path/"library.db"); initialize_library(connection,migrated_at="2026-07-17T00:00:00+00:00")
    repo=LibraryRepository(connection); now=datetime(2026,7,17,tzinfo=timezone.utc)
    repo.upsert_source(source_id="gmail-newsletters",provider="gmail_newsletter",display_name="Gmail",source_type="newsletter",tier=.7,now=now)
    repo.record_newsletter_issue(source_id="gmail-newsletters",mailbox="ResearchFeeds",uid="1",body_hash="hash",parser_id="tldr-v1",created_at=now,entries=({"title":"Launch","tracked_url":"https://short.example/x","canonical_url":"https://short.example/x","content_type":"product_release","classification_confidence":.8},))
    return connection,now


def test_enrichment_dry_run_is_non_mutating_then_persists_cache(tmp_path):
    connection,_=_staged(tmp_path); service=NewsletterEnrichmentService(connection,resolver=Resolver(),metadata_extractor=Metadata())
    assert service.enrich(dry_run=True).resolved == 1
    assert connection.execute("select resolution_status from newsletter_entries").fetchone()[0] == "pending"
    summary=service.enrich(); assert (summary.resolved,summary.metadata_fetched)==(1,1)
    assert connection.execute("select final_url from url_resolutions").fetchone()[0] == "https://example.com/article"
    assert connection.execute("select title from page_metadata").fetchone()[0] == "Agent launch"
    connection.close()


def test_promotion_dry_run_then_creates_one_library_item_and_backlink(tmp_path):
    connection,now=_staged(tmp_path); NewsletterEnrichmentService(connection,resolver=Resolver(),metadata_extractor=Metadata()).enrich(now=now)
    promoter=NewsletterPromoter(connection)
    dry=promoter.promote(dry_run=True,now=now); assert dry.new==1
    assert connection.execute("select count(*) from research_items").fetchone()[0]==0
    result=promoter.promote(now=now); assert result.new==1
    row=connection.execute("select research_item_id from newsletter_entries").fetchone(); assert row[0]
    assert connection.execute("select title from research_items").fetchone()[0]=="Agent launch"
    connection.close()


def test_promotion_with_topic_policies_skips_irrelevant_entry(tmp_path):
    connection,now=_staged(tmp_path); NewsletterEnrichmentService(connection,resolver=Resolver(),metadata_extractor=Metadata()).enrich(now=now)
    result=NewsletterPromoter(connection).promote(
        now=now, topic_policies={"unrelated": {"include": ("quantum chemistry",), "exclude": ()}},
    )
    assert result.skipped == 1
    assert connection.execute("select count(*) from research_items").fetchone()[0] == 0
    connection.close()


def test_topic_matching_uses_token_overlap_and_type_aware_agent_fallback():
    from research_copilot.library import ResearchItemDraft
    draft = ResearchItemDraft(
        title="How I cut an AI agent's token use", item_type="engineering",
        summary="A compiled workflow reduced token costs.",
    )
    matches = NewsletterPromoter._match_topics(draft, {
        "agent-infra": {"include": ("agent runtime", "tool orchestration"), "exclude": ()},
        "inference": {"include": ("KV cache optimization",), "exclude": ()},
    })
    assert [match.topic_id for match in matches] == ["agent-infra"]
