# Hermes Next Steps TODO

Status: MOVE and quarantine-first DELETE are implemented, test-backed, and
validated in disposable `/tmp` pilots. This file lists only the remaining
operational work.

## 1. Audit Operator Skills

- [ ] Compare `ce_library_manager`, `ce_library_governed_move`, and
  `ce_library_governed_delete` instructions against their current Python CLI
  commands.
- [ ] Confirm every mutation workflow says: dry-run first, explicit
  `--execute`, explicit paths and IDs, exact-operation recovery only.
- [ ] Confirm the DELETE skill prohibits permanent deletion and requires an
  explicitly selected retained copy.

## 2. Establish Release Baseline

- [ ] Run the full test suite with `./venv/bin/python -m pytest -q`.
- [ ] Review the dirty worktree and separate unrelated changes from governed
  file-management changes.
- [ ] Record the exact test result and intended commit contents before making
  a release commit.

## 3. Controlled Real MOVE Pilot

- [ ] Select one non-critical indexed file and an approved destination.
- [ ] Run the MOVE command as dry-run and review the plan/result JSON.
- [ ] Create and validate the required MOVE approval artifact.
- [ ] Execute once with `--execute`.
- [ ] Verify filesystem hash, registry locator state, tracker paths, Chroma
  metadata, and `VERIFIED` journal phase.

## 4. Controlled Real Quarantine Pilot

- [ ] Select one non-critical exact duplicate and explicitly choose the
  retained copy and quarantine destination.
- [ ] Run the quarantine command as dry-run and review the result JSON.
- [ ] Create and validate the required quarantine approval artifact.
- [ ] Execute once with `--execute`.
- [ ] Verify retained bytes, quarantined bytes, inactive target locator,
  tracker/Chroma retained path, and `VERIFIED` journal phase.

## 5. Closeout

- [ ] Save both pilot reports with operation IDs and verification evidence.
- [ ] Fix the test-only `datetime.utcnow()` deprecation warning when
  convenient.
- [ ] Update the Hermes roadmap to mark implementation phases A-G complete.

## Explicitly Deferred

- Permanent deletion.
- Bulk move or delete.
- Automatic routing or autonomous execution.
- Approval-artifact creation interface.
- Production rollout beyond approved one-file pilots.
