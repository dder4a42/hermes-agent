from datetime import datetime, timedelta, timezone

from research_copilot.library import LibraryRepository, ResearchItemDraft, SourceEvidence, connect_library, initialize_library
from research_copilot.reporting import DailyReportService, render_daily_report


def test_daily_report_groups_types_and_ignores_old_items(tmp_path):
    connection=connect_library(tmp_path/"library.db"); initialize_library(connection,migrated_at="2026-07-17T00:00:00+00:00")
    repo=LibraryRepository(connection); now=datetime(2026,7,17,tzinfo=timezone.utc)
    repo.upsert_source(source_id="s",provider="rss",display_name="S",source_type="feed",tier=.8,now=now)
    for title,kind,when in (("Paper","paper",now),("News","research_news",now),("Opinion","opinion",now),("Old","paper",now-timedelta(days=3))):
        repo.upsert_item(ResearchItemDraft(title=title,item_type=kind,url=f"https://example.com/{title}"),source=SourceEvidence("s"),discovered_at=when)
    report=DailyReportService(connection).build(generated_at=now,days=1)
    assert [x.title for x in report.papers]==["Paper"]
    assert [x.title for x in report.news]==["News"]
    assert [x.title for x in report.opinions]==["Opinion"]
    rendered=render_daily_report(report); assert "Must-read papers" in rendered and "Old" not in rendered
    assert "paper" in rendered
    connection.close()
