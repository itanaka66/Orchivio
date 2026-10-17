# ADR-0006: Open-source licence

Status: **Accepted** · Date: 2026-10-17 · DESIGN.md §18, §22 item 6

## Decision
Orchivio is licensed under the **GNU General Public License v3.0 or later (GPL-3.0-or-later)**.
Copyright (C) 2026 株式会社エージーネディア / agNedia Inc. The full text is in `LICENSE`.

## Context
DESIGN.md §18.1 asks for a deliberate choice between permissive and copyleft licences and for
the core, plugins, generated artifacts and examples to be reviewed separately.

## Consequences
- Anyone distributing a modified Orchivio must provide the corresponding source under the GPL.
  GPL-3.0 does **not** by itself cover use over a network (that is AGPL territory), so a hosted
  fork is not obliged to publish its changes. Revisit if that matters.
- Dependencies must be GPL-3.0-compatible. Check every new dependency's licence (CONTRIBUTING.md);
  generate the SBOM and `NOTICE` before a release.
- Contributions are accepted under the same licence. A contributor agreement (DCO or CLA) is
  still undecided; settle it before accepting substantial outside contributions, because
  relicensing later requires every contributor's consent.

## Still to clarify (not decided by this ADR)
1. **Generated applications and exported models.** The GPL applies to Orchivio's own code. Users
   must be able to ship what they build, so state explicitly that application models, generated
   code and generated artifacts are *not* covered by the platform's licence, and take care that
   any code the generator copies from Orchivio into its output (templates, runtime libraries)
   is under terms that allow this (for example the GPL runtime-library exception or a separate
   permissive licence for those parts).
2. **Plugins, components and the module registry** (Phase 12): licence requirements and the
   linking boundary.
3. **Trademark:** the licence grants no rights to the name; clearance is a separate item (§18.2).

Item 1 should be settled before the first public release. This is a legal matter; seek advice
where appropriate.
