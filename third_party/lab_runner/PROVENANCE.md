# harvey-labs runner assets

These runners derive from the harvey-labs integration and retain its
[upstream license](LICENSE). See the [pinned revision and patches](../../../dependencies/PINNED.md).

The integration adds editable harness installation and configurable models,
paths, and endpoints. Local model-provider names, environment variables, and
log paths use the `harness2` namespace. Setup installs these runners into the LAB checkout as overlays.

[ASSETS.json](../../../dependencies/ASSETS.json) records the shipped file checksums.
Verify them with `python scripts/check_provenance.py`.

`claude_code/run_claude_code.py` (added by SELF-AKDO, 2026-10-22) runs a LAB task through Claude
Code. It mirrors `opencode/run_opencode.py` (task loading, prompt wording, results layout,
document-coverage rule) and splits the run into `prepare` (needs the LAB checkout) and `run`
(needs only the workspace), so a deployment can keep `task.json` and its criteria out of the
agent's container.
