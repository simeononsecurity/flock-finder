# Repository History Cleanup Plan

Status: proposal only. No history rewrite is authorized or performed by this plan.

Measured on 2026-10-14 from commit `593f34a4576616fd2a85a55e8c6f19711824bf4c`.

## Baseline

| Measurement | Result |
| --- | ---: |
| Commits reachable from all local refs | 145 |
| Checkout size | 1.8 GB |
| `.git` size | 1.6 GB |
| Loose Git objects | 3,022 objects, 1.25 GiB |
| Packed Git objects | 1,206 objects, 331.62 MiB |
| Current ignored generated data | 221 MB |
| Historical `data/flock_cameras.geojson` revisions | 76 |
| Historical `data/flock_cameras.csv` revisions | 69 |
| Historical `data/by_oui/` revisions | 64 |

The repeated uncompressed blob totals show the main growth sources:

| Historical path | Revisions | Sum of historical blob sizes |
| --- | ---: | ---: |
| `data/flock_cameras.geojson` | 74 blobs | 4.39 GB |
| `data/flock_cameras.csv` | 67 blobs | 1.12 GB |
| `data/by_oui/08_3A_88.geojson` | 62 blobs | 671 MB |
| `data/by_oui/E0_4F_43.geojson` | 50 blobs | 468 MB |
| `data/by_oui/74_4C_A1.geojson` | 62 blobs | 438 MB |

These totals are uncompressed historical payload, not current disk use. Git compression and delta storage account for the smaller 1.6 GB object database.

## Proposed scope

Remove only generated dataset history from a fresh mirror clone:

- `data/flock_cameras.geojson`
- `data/flock_cameras.csv`
- `data/by_oui/**`

Keep source, tests, documentation, curated OUI metadata, scan statistics, and release manifests. Confirm the final path list with a `git filter-repo --analyze` report before any rewrite.

Do not combine dependency updates, application changes, or data-policy changes with the rewrite.

## Approval gate

The repository owner must approve all of the following before execution:

1. Rewrite every affected branch and tag.
2. Force-update the remote refs with lease protection.
3. Freeze scheduled data jobs and repository writes during cutover.
4. Replace or update the `data-latest` release tag without losing release assets.
5. Notify every contributor to replace old clones or rebase work onto the new history.

Without this approval, stop after measurement and review of this plan.

## Backup and archive

Perform backup work in a new directory outside every existing checkout.

1. Create a mirror clone of the remote.
2. Record the original default-branch commit and every branch and tag ref.
3. Create an annotated archive tag named `archive/pre-history-cleanup-YYYYMMDD` at the original default-branch commit.
4. Push the archive tag to a separate private archive mirror, not the rewritten primary repository.
5. Create a Git bundle containing all original refs.
6. Calculate and record SHA-256 hashes for the bundle and a ref inventory.
7. Test the bundle by cloning into a separate temporary directory and checking out the recorded commit.

Keeping the archive ref on the primary repository would keep the unwanted objects reachable and defeat the size reduction.

## Rewrite rehearsal

Use a disposable mirror clone. Never run history rewriting commands in the working checkout.

1. Run `git filter-repo --analyze` and review the path and blob reports.
2. Rewrite only the approved generated paths with `git filter-repo` path filters and `--invert-paths`.
3. Record old-to-new commit mappings from the filter-repo output.
4. Expire and repack only the disposable mirror after the rewritten refs pass inspection.
5. Compare branch names, tags, release tag requirements, and default-branch configuration with the captured ref inventory.
6. Measure the rewritten object database and a fresh working clone.

The rehearsal must stop if source files, documentation, curated metadata, or required tags disappear.

## Verification before cutover

Create a normal clone from the rewritten rehearsal and verify:

1. The current source tree matches the original current source tree, excluding only the approved historical generated paths.
2. No generated dataset path appears in reachable history.
3. Repository tests, Ruff, data validation, snapshot verification, and browser smoke tests pass.
4. Release and Pages workflows reference release assets or immutable snapshots instead of historical Git data.
5. The `data-latest` release assets remain recoverable from the archive and from the planned post-cutover release flow.
6. A secret scan finds no credentials in retained history.
7. Fresh clone size and checkout time are recorded.

## Contributor notice

Send a notice before cutover with:

- The maintenance window and write freeze.
- The final pre-rewrite commit and archive tag.
- The reason for removing generated data history.
- The affected paths.
- A warning that every rewritten commit hash will change.
- Instructions to preserve unmerged work as patches or topic branches.
- Instructions to replace old clones after cutover.
- A contact path for recovery help.

Send a second notice after cutover with the new default-branch commit, fresh-clone instructions, and the archive location.

## Commit-hash impact

Every commit at or after the first removed generated-data revision receives a new hash. Open pull requests, pinned commit links, local branches, forks, release tags, and external references to old commits require review. Contributors must not push old rewritten branches back to the primary repository.

## Cutover

Cutover requires a second explicit approval after the rehearsal report.

1. Freeze scheduled workflows and repository writes.
2. Fetch the remote again and verify the remote refs still match the approved inventory.
3. Update rewritten branches and tags with force-with-lease semantics.
4. Restore the rolling dataset release and Pages configuration.
5. Re-enable workflows only after remote checks and a fresh clone pass.
6. Publish the post-cutover contributor notice.

## Rollback

If verification fails after cutover:

1. Freeze writes again.
2. Restore the original refs from the verified mirror or Git bundle.
3. Restore the original `data-latest` tag and release association.
4. Verify the original default-branch commit and release asset hashes.
5. Re-enable workflows only after a fresh clone and site-data check pass.

The archive mirror and bundle must remain unchanged until the owner accepts the rewritten repository and the contributor recovery window ends.

## Final acceptance evidence

The execution report must include:

- Original and rewritten ref inventories.
- Original and rewritten repository sizes.
- Old and new default-branch commit IDs.
- Archive tag, archive location, bundle hash, and restore test result.
- Removed-path verification.
- Full test, validation, browser, and fresh-clone results.
- Release asset and Pages checks.
- Contributor notice timestamps.
- Owner approval for rehearsal and cutover.
