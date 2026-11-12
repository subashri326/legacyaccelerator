"""Git delivery: wave branch commits and pull-request metadata generation (Section 4.3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from git import Actor, Repo


@dataclass
class GitDeliveryResult:
    branch_name: str
    commit_sha: str
    pr_title: str
    pr_body: str
    pr_url_or_path: str
    files_written: list[str]


class GitDeliveryService:
    """Commit generated wave artifacts to a Git repository on a dedicated wave branch."""

    def __init__(self, repo_path: str | Path, base_branch: str = "main") -> None:
        self.repo_path = Path(repo_path).resolve()
        self.base_branch = base_branch

    def _ensure_repo(self) -> Repo:
        self.repo_path.mkdir(parents=True, exist_ok=True)
        if not (self.repo_path / ".git").exists():
            repo = Repo.init(self.repo_path, initial_branch=self.base_branch)
            readme = self.repo_path / "README.md"
            if not readme.exists():
                readme.write_text("# Modernized Target Repository\n", encoding="utf-8")
            repo.index.add(["README.md"])
            author = Actor("L2C Accelerator", "l2c-bot@accelerator.local")
            repo.index.commit("chore: initialize target repo", author=author, committer=author)
            return repo
        return Repo(self.repo_path)

    def deliver_wave(
        self,
        wave_name: str,
        branch_prefix: str,
        files: dict[str, str],
        summary_lines: list[str] | None = None,
    ) -> GitDeliveryResult:
        repo = self._ensure_repo()
        safe_wave = wave_name.lower().replace(" ", "-").replace("/", "-")
        branch_name = f"{branch_prefix.rstrip('/')}/{safe_wave}"

        # Ensure base branch exists and checkout/create wave branch idempotently
        if self.base_branch in repo.heads:
            repo.heads[self.base_branch].checkout()
        if branch_name in repo.heads:
            repo.heads[branch_name].checkout()
        else:
            repo.create_head(branch_name).checkout()

        written: list[str] = []
        for rel_path, content in files.items():
            clean_rel = rel_path.lstrip("/")
            full_path = self.repo_path / clean_rel
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            written.append(clean_rel)

        if written:
            repo.index.add(written)

        author = Actor("L2C Accelerator", "l2c-bot@accelerator.local")
        pr_title = f"feat(migration): deliver {wave_name} generated target artifacts"
        bullets = "\n".join(f"- {line}" for line in (summary_lines or []))
        pr_body = (
            f"## Wave Delivery: {wave_name}\n\n"
            f"Generated {len(written)} artifact(s) by Legacy-to-Cloud Accelerator.\n\n"
            f"{bullets}\n"
        )

        if repo.is_dirty(index=True, working_tree=True, untracked_files=True):
            commit = repo.index.commit(pr_title, author=author, committer=author)
            commit_sha = commit.hexsha
        else:
            commit_sha = repo.head.commit.hexsha

        pr_meta_path = self.repo_path / ".l2c_prs" / f"{safe_wave}.json"
        pr_meta_path.parent.mkdir(parents=True, exist_ok=True)
        pr_meta_path.write_text(
            json.dumps(
                {
                    "title": pr_title,
                    "source_branch": branch_name,
                    "target_branch": self.base_branch,
                    "commit_sha": commit_sha,
                    "files": written,
                    "body": pr_body,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        return GitDeliveryResult(
            branch_name=branch_name,
            commit_sha=commit_sha,
            pr_title=pr_title,
            pr_body=pr_body,
            pr_url_or_path=str(pr_meta_path),
            files_written=written,
        )
