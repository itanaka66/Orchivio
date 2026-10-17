# ADR-0006: Open-source licence

Status: **Open — needs the project owner** · Date: 2026-10-17 · DESIGN.md §18, §22 item 6

## Context
The repository's `LICENSE` file is the GNU GPL v3 text (it came from repository creation). The
design asks for a deliberate choice (§18.1): compare permissive (MIT/Apache-2.0) with copyleft
options, and review the core, plugins, generated artifacts and examples separately.

## Questions to settle
1. Should the **core** be permissive (maximum adoption, allows closed forks and hosted
   forks) or copyleft (GPL-3.0 / AGPL-3.0, which closes the network-service gap if hosted use
   matters)?
2. Is **generated application code and the exported model** guaranteed free of the platform's
   licence obligations? (Strongly recommended; otherwise users cannot confidently ship what they
   build.)
3. Licence for **plugins/components** and the future registry.
4. Contributor terms (DCO or CLA) before accepting outside contributions.

## Interim position
No code is being relicensed. Until this ADR is decided, the repository stays under the GPL v3
text already present, `pyproject.toml` points at that file, and no claim is made about a final
licence. Check dependency licences for compatibility with whichever licence is chosen (SBOM and
`NOTICE` are release requirements, §18.1).

This is a legal and strategic decision; seek advice where appropriate.
