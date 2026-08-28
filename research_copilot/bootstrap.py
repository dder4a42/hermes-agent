"""Research Copilot profile bootstrap helpers.

These helpers are deliberately profile-home based so one Weixin bot can map to
one Hermes profile without sharing paper state, scripts, or cron jobs.

Bootstrap is idempotent by default: existing user files (JSON/JSONL under
``research-copilot/``) are never overwritten unless ``force=True`` is passed.
Skill and script assets, which are versioned code rather than user state, are
refreshed on every run so a profile picks up upstream fixes automatically.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_JSON_FILES = {
    "config.json": '{\n  "pipeline": "research_signal_profile_aware"\n}\n',
    "topics.json": '{\n  "topics": []\n}\n',
    "source_registry.json": '{\n  "sources": []\n}\n',
    "research_profile.json": '{\n  "long_term_agenda": []\n}\n',
    "state.json": '{\n  "last_fetch_at": null,\n  "last_recommendation_at": null\n}\n',
}
_JSONL_FILES = ("candidates.jsonl", "recommendations.jsonl", "interactions.jsonl")
_SCRIPT_FILES: tuple[str, ...] = ()
_LEGACY_SCRIPT_FILES = ("paper-fetch.py", "paper-health.py")
_OPTIONAL_SCRIPT_FILES = ("task-surfacer.py", "thought-surfacer.py")

# Skill assets installed into the profile's ~/.hermes/skills/research/paper/
# tree so interactive paper commands remain available under the profile.
_SKILL_ROOT = "skills/research/paper"
_SKILL_FILES = (
    "SKILL.md",
    "references/format.md",
    "references/paper-feedback-loop.md",
    "references/sources.md",
    "references/profile-aware-research-copilot.md",
    "references/research-signal-pipeline.md",
    "references/companion-systems.md",
    "references/weixin-profile-research-copilot.md",
    "references/gfw-setup.md",
    "references/gmail-setup.md",
    "references/world-model-inference-optimization.md",
)

_CRON_NAMES = (
    "paper-fetcher", "research-library-recommend", "paper-health-report",
    "task-surfacer", "thought-surfacer",
)

_RESEARCH_RECOMMEND_PROMPT = """你是用户的科研资讯编辑。预运行脚本会提供一条 JSON 格式的候选文章、匹配话题、排序依据和科研画像。

请只根据这些材料写一条适合微信阅读的中文推荐，要求：
1. 主体叙述和所有栏目标题使用中文；论文或项目的原始标题可以保留原文。
2. 先用 2—3 句话准确概述工作，不要把摘要中没有的内容写成事实。
3. 明确说明它对应用户的哪些兴趣、开放问题或知识缺口。
4. 结合 current_beliefs 给出有立场的点评：它支持、挑战还是补充了什么观点；若材料不足，明确说“仅凭当前摘要尚不能判断”。
5. 区分论文主张、证据和你的推断；指出一个最值得关注之处和一个局限或验证问题。
6. 不展示 JSON、内部评分维度或英文模板字段，不调用工具，不自行搜索网络。

固定格式：
🔬 今日科研推荐
《原始标题》

内容概述：……

与你的研究的关系：……

我的点评：……

值得继续追问：……

原文：URL
"""

# Repository asset roots. Bootstrap prefers ``source_home`` (a live profile
# used as a template) when supplied, then falls back to these repo-owned
# copies so a first-time install always works even without a template.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO_SCRIPTS_ROOT = _REPO_ROOT / "research_copilot" / "scripts"
_REPO_SKILL_ROOT = _REPO_ROOT / _SKILL_ROOT


def _script_repo_name(filename: str) -> str:
    """Map an installed script filename (paper-fetch.py) to its repo counterpart
    (paper_fetch.py). The dashed names live under ~/.hermes/scripts/; the
    underscored names are the Python module-safe repo copies."""
    return filename.replace("-", "_")


def _copy_if_missing_or_force(src: Path, dst: Path, *, force: bool) -> bool:
    """Copy ``src`` to ``dst``. Returns True if the file was written."""
    if not src.exists():
        return False
    # A profile may link managed assets back to their repository-owned source.
    # copy2() raises SameFileError in that already-up-to-date state, breaking
    # otherwise-idempotent bootstrap runs.
    try:
        if dst.exists() and src.samefile(dst):
            return False
    except OSError:
        pass
    if dst.exists() and not force:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _script_source(filename: str, source: Path | None) -> Path | None:
    """Locate the best source for a script asset.

    Preference order:
      1. ``<source>/scripts/<filename>`` (live template profile)
      2. ``<repo>/research_copilot/scripts/<repo-name>`` (repo-owned canonical)
    """
    if source is not None:
        candidate = source / "scripts" / filename
        if candidate.exists():
            return candidate
    repo = _REPO_SCRIPTS_ROOT / _script_repo_name(filename)
    if repo.exists():
        return repo
    repo = _REPO_ROOT / "scripts" / _script_repo_name(filename)
    if repo.exists():
        return repo
    return None


def _skill_source(rel: str, source: Path | None) -> Path | None:
    """Locate the best source for a skill asset (SKILL.md / references/*.md)."""
    if source is not None:
        candidate = source / _SKILL_ROOT / rel
        if candidate.exists():
            return candidate
    repo = _REPO_SKILL_ROOT / rel
    if repo.exists():
        return repo
    return None


def initialize_research_copilot_home(
    profile_home: str | Path,
    *,
    source_home: str | Path | None = None,
    force: bool = False,
    optional_scripts: Iterable[str] = _OPTIONAL_SCRIPT_FILES,
) -> dict[str, Any]:
    """Initialize a profile's Research Copilot files, scripts, and skill assets.

    Args:
        profile_home: Path to the target profile's HERMES_HOME.
        source_home: Optional live profile whose state to use as a template.
            When present, JSON files are copied from there (instead of the
            neutral defaults) and scripts/skills are preferred from there
            over the repo-owned canonical copies.
        force: When ``True``, existing user JSON files are overwritten from
            the source or default. JSONL user data files (candidates,
            recommendations, interactions) are always preserved regardless
            of ``force`` to prevent history loss.
        optional_scripts: Extra script filenames to install if present in
            the source. Defaults to the surfacer scripts.

    Returns a small report describing what changed.
    """
    profile_home = Path(profile_home).expanduser().resolve()
    source = Path(source_home).expanduser().resolve() if source_home else None
    data_dir = profile_home / "research-copilot"
    scripts_dir = profile_home / "scripts"
    skills_dir = profile_home / "skills" / "research" / "paper"
    data_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    created: list[str] = []
    refreshed: list[str] = []
    removed: list[str] = []
    skipped: list[str] = []

    # These were versioned Research Copilot implementation files copied into
    # every profile.  Bundled cron modules now own the implementation, so the
    # profile contains state and job definitions only.
    for filename in _LEGACY_SCRIPT_FILES:
        legacy = scripts_dir / filename
        if legacy.is_file():
            legacy.unlink()
            removed.append(f"scripts/{filename}")

    for filename, default_content in _DEFAULT_JSON_FILES.items():
        dst = data_dir / filename
        src = source / "research-copilot" / filename if source else None
        exists = dst.exists()
        if exists and not force:
            skipped.append(f"research-copilot/{filename}")
            continue
        if src and src.exists():
            shutil.copy2(src, dst)
            (refreshed if exists else copied).append(f"research-copilot/{filename}")
        else:
            dst.write_text(default_content, encoding="utf-8")
            (refreshed if exists else created).append(f"research-copilot/{filename}")

    # JSONL user-data files (NEVER overwritten, even with --force).
    for filename in _JSONL_FILES:
        dst = data_dir / filename
        if dst.exists():
            skipped.append(f"research-copilot/{filename}")
            continue
        dst.write_text("", encoding="utf-8")
        created.append(f"research-copilot/{filename}")

    # Scripts — refreshed idempotently (code, not user state).
    for filename in _SCRIPT_FILES:
        src = _script_source(filename, source)
        if src is None:
            skipped.append(f"scripts/{filename} (no source)")
            continue
        dst = scripts_dir / filename
        existed = dst.exists()
        if _copy_if_missing_or_force(src, dst, force=True):
            (refreshed if existed else copied).append(f"scripts/{filename}")

    for filename in optional_scripts:
        src = _script_source(filename, source)
        if src is None:
            continue
        dst = scripts_dir / filename
        existed = dst.exists()
        if _copy_if_missing_or_force(src, dst, force=True):
            (refreshed if existed else copied).append(f"scripts/{filename}")

    # Skill assets — refreshed idempotently.
    for rel in _SKILL_FILES:
        src = _skill_source(rel, source)
        if src is None:
            continue
        dst = skills_dir / rel
        existed = dst.exists()
        if _copy_if_missing_or_force(src, dst, force=True):
            (refreshed if existed else copied).append(f"{_SKILL_ROOT}/{rel}")

    return {
        "profile_home": str(profile_home),
        "data_dir": str(data_dir),
        "created": created,
        "copied": copied,
        "refreshed": refreshed,
        "removed": removed,
        "skipped": skipped,
    }


def _existing_job_names() -> set[str]:
    from cron.jobs import list_jobs

    return {str(job.get("name") or "") for job in list_jobs(include_disabled=True)}


def install_research_copilot_cron(
    profile_home: str | Path,
    *,
    deliver: str = "weixin",
    force: bool = False,
) -> dict[str, Any]:
    """Create the standard Research Copilot cron jobs in one profile store.

    Idempotent by default: existing jobs with the same names are left
    untouched. Bootstrap of the underlying profile files uses the same
    ``force`` semantics as ``initialize_research_copilot_home``.
    """
    from cron.jobs import create_job, list_jobs, update_job, use_cron_store

    profile_home = Path(profile_home).expanduser().resolve()
    initialize_research_copilot_home(profile_home, force=force)

    created: list[str] = []
    existing: list[str] = []

    with use_cron_store(profile_home):
        names = _existing_job_names()
        if "paper-fetcher" in names:
            existing.append("paper-fetcher")
            job = next(
                j for j in list_jobs(include_disabled=True)
                if j.get("name") == "paper-fetcher"
            )
            update_job(job["id"], {
                "script": "module:research_copilot.scripts.library_collect",
                "no_agent": True,
            })
        else:
            create_job(
                prompt=None,
                schedule="0 6,18 * * *",
                name="paper-fetcher",
                deliver=deliver,
                script="module:research_copilot.scripts.library_collect",
                no_agent=True,
            )
            created.append("paper-fetcher")

        if "research-library-recommend" in names:
            existing.append("research-library-recommend")
            job = next(
                j for j in list_jobs(include_disabled=True)
                if j.get("name") == "research-library-recommend"
            )
            update_job(job["id"], {
                "prompt": _RESEARCH_RECOMMEND_PROMPT,
                "script": "module:research_copilot.scripts.library_recommend",
                "no_agent": False,
            })
        else:
            create_job(
                prompt=_RESEARCH_RECOMMEND_PROMPT,
                schedule="30 8 * * *",
                name="research-library-recommend",
                deliver=deliver,
                script="module:research_copilot.scripts.library_recommend",
                no_agent=False,
            )
            created.append("research-library-recommend")

        if "paper-health-report" in names:
            existing.append("paper-health-report")
            job = next(
                j for j in list_jobs(include_disabled=True)
                if j.get("name") == "paper-health-report"
            )
            update_job(job["id"], {
                "script": "module:research_copilot.scripts.library_health",
                "no_agent": True,
            })
        else:
            create_job(
                prompt=None,
                schedule="0 9 * * 6",
                name="paper-health-report",
                deliver=deliver,
                script="module:research_copilot.scripts.library_health",
                no_agent=True,
            )
            created.append("paper-health-report")

        if "task-surfacer" in names:
            existing.append("task-surfacer")
        else:
            create_job(
                prompt=None, schedule="every 5m", name="task-surfacer",
                deliver=deliver, script="task-surfacer.py", no_agent=True,
            )
            created.append("task-surfacer")

        if "thought-surfacer" in names:
            existing.append("thought-surfacer")
        else:
            create_job(
                prompt=None, schedule="every 30m", name="thought-surfacer",
                deliver=deliver, script="thought-surfacer.py", no_agent=True,
            )
            created.append("thought-surfacer")

    return {
        "profile_home": str(profile_home),
        "created": created,
        "existing": existing,
    }
