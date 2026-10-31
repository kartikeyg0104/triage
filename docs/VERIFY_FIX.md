# Verify a fix claim across revisions

A PR that says "fixes #120" makes a claim that can be tested: the repro should fail on the base revision and pass on the PR head.

```bash
python -m claimcheck check \
  --repo . --report pr.txt \
  --repro "python3 poc.py" --expect "buffer overflow" \
  --base main --head fix-120
```

`--base` and `--head` take any git ref (branch, tag, SHA, `HEAD~1`) and must be given together, with `--repro`. Otherwise the command exits 2. `--executor kubernetes` works the same way: each side runs as its own Job.

## How it runs

- Both refs are resolved to full commit SHAs with `git rev-parse --verify <ref>^{commit}`.
- Each SHA is checked out as a detached `git worktree` in a temp dir, with git hooks disabled. Your checkout, branch, index and working tree are not touched, and uncommitted edits do not leak into either side.
- The repro runs in that worktree with the chosen executor, which judges each side exactly as it judges a single `--repro`.
- Each worktree is removed afterwards, even if the executor fails. `git worktree prune` only runs if removing ours failed, because it would also drop your own stale worktrees.
- If `--repo` is a subdirectory of the git repo, the repro runs in the same subdirectory of each revision.

The command comes only from `--repro`. Text in the report or PR is never run.

## Outcomes

| Base | Head | Result |
|---|---|---|
| fails with the expected text | exits 0 | **confirmed**: fix confirmed |
| fails with the expected text | fails with the expected text | **refuted**: not fixed |
| exits 0 | anything | unverified: bug not reproduced on base |
| setup failure, timeout, other failure, refused | anything | unverified: no clean reproduction on base |
| fails with the expected text | setup failure, timeout, other failure, refused | unverified: head gave no clean result |
| bad ref, not a git checkout, subdirectory missing at a revision | | unverified: setup failure |

The result replaces the single repro row in the report. The detail names both full SHAs and gives each side's own result. The source reads `base <sha12> → head <sha12>; <executor>`. The basis is always `observed`.

## Limits

- Without `--expect`, base can never count as reproduced, so the result is always unverified.
- Submodules are not checked out in the worktrees, and LFS or other filters run as your git config says.
- Like the single repro, this shows that the expected failure appeared and then went away. It does not show that the fix is correct in general.
