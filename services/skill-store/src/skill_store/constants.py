"""Cross-module constants for skill-store.

``PLATFORM_TENANT_ID`` is the reserved, synthetic tenant that owns the
*platform-global default skill set*: a single set of skills every organization
(tenant) — including tenants provisioned in the future — automatically sees and
every user gets by default with **zero** per-user install rows.

Platform rows are:

* Readable by any tenant — the ``skills_tenant_select`` RLS policy is broadened
  (migration ``0004``) to allow ``tenant_id = PLATFORM_TENANT_ID`` regardless of
  the caller's ``app.tenant_id``.
* Writable ONLY when ``app.tenant_id`` equals this sentinel — the insert/update/
  delete policies are unchanged, so only the platform seed (which pins
  ``X-Tenant-Id = PLATFORM_TENANT_ID``) can create/mutate them.

The literal must be byte-for-byte identical everywhere it appears (this module,
the ``0004`` migration SQL, and the seed script) — it is a data value, not a
config knob.
"""

from __future__ import annotations

# Reserved sentinel tenant that owns the platform-global default skills.
# NOTE: keep this literal in sync with the value hard-coded in the
# ``0004_platform_default_skills`` migration SQL.
PLATFORM_TENANT_ID = "tnt_00000000000000000PLATFORM"
