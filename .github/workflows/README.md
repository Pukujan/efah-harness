# CI workflows

Contract EFAH-CONTRACT-001 v1.1 Section 21. CI is the release authority: it
performs the merge, and the implementing agent does not self-certify.

**GATE-D1-07 A4 invariant:** no workflow here may require Claude access. The
vendor-neutrality job asserts this mechanically over this directory, so adding a
Claude-dependent step fails the build rather than passing quietly.
