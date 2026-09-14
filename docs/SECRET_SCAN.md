# Local content and secret checks

A pattern scan is one check, not proof that transcript-derived text is public.
Review every file intended for sharing, including comments, fixtures, images and
generated assets. Keep raw findings and copied evidence outside Git.

The audit script requires Gitleaks 8.30.1 and a new private output directory:

```sh
uv run python ops/scan-secrets.py   --scanner /absolute/path/to/gitleaks   --output /absolute/private/new-source-audit
```

It snapshots tracked files, verifies their hashes and scans those exact bytes.
The optional `--history` also scans locally reachable history and refuses a change
to refs during that scan. A flagged value returns a nonzero result. Review the
private report; do not treat a scanner execution failure as zero findings.

The configuration retains default rules and adds detection of short Figma-token
shapes. Fixture exceptions bind both a reviewed exact value and its file.
Do not broaden exceptions merely to obtain a passing scan.

For a release, inspect expanded wheels and source distributions as well as source.
A scan of an archive filename alone does not establish member coverage. Verify
extraction paths, every member and the source-to-package asset mapping. Run a
planted-token control on each scanned form to show the scanner can detect a
finding in that path, then preserve the clean artifact's original bytes.

No scanner report, copied private input or detailed source provenance belongs in
the public repository. See [security reporting](../SECURITY.md).
