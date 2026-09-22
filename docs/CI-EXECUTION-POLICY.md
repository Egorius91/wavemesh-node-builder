# CI execution policy

Related: wavevpn-saas#322. This repository uses PR review followed by main verification. `tests` runs on pull requests and main pushes, not on the feature push as well. A feature branch without a PR has no automatic `tests` run: perform local checks and open a PR (including a draft PR) before claiming CI proof. No path/test filtering is introduced.

Only an obsolete run for the same PR may be cancelled. Main verification is isolated by event/ref and is not cancelled by PR updates. GitHub concurrency may replace pending work in a group; neither pending nor cancelled work is CI proof. Any release still requires successful checks for its exact target, not a previous SHA. CodeQL, full-history Gitleaks and ShellCheck workflows remain unchanged, including schedules and permissions.

All existing nginx, Python, mTLS/runtime, E2E, Bash, adapter, installer, transaction and rollback checks remain in the same job with the same settings. The small policy test is picked up by the existing unit-test loop, without a new job. Do not make empty commits or rerun successful jobs to populate a cache or refresh evidence. Failure investigation precedes a targeted retry. Repository visibility and billing are not changed; fewer redundant runs is not a claim of dollar savings for standard runners in this public repository.

Rollback: revert this isolated CI policy change. No database, environment, deployment, node command or runtime migration is needed.
