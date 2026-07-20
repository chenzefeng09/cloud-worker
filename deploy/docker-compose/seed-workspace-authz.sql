-- ============================================================================
-- workspace authz seed — dev stack (foundation postgres)
--
-- Registers the `workspace` resource type in the authz catalog and grants the
-- new permissions to the roles that back the workspace endpoints in
-- session-orchestrator:
--   * require_permission("workspace.read",   "workspace")  (GET)
--   * require_permission("workspace.manage", "workspace")  (POST/PATCH/DELETE + file upload/delete)
--
-- Mirrors the source-of-truth changes already made in saas-foundation:
--   * packages/authz/migrations/versions/2026_07_16_0009-0009_workspace_perms.py
--     (catalog insert + array_append back-fill onto existing member/tenant_admin
--     roles — same pattern as 0005/0008; running the authz migration-runner to
--     head applies the equivalent of this file)
--   * packages/authz/scripts/bootstrap_tenant_roles.py
--     (member/tenant_admin permission lists for freshly provisioned tenants)
--
-- SAFETY: append-only. The two INSERTs use ON CONFLICT DO NOTHING. The role
-- grants use `permissions || ARRAY[...]` guarded by a `NOT (permissions @> ...)`
-- WHERE clause, so they only ADD the new tokens to the existing array and never
-- remove or rewrite any existing permission. Re-running is idempotent.
--
-- NOTE: authz has no role_permission join table — a role's permissions are a
-- text[] array column on authz.role. Adding a permission to a role is therefore
-- necessarily an UPDATE that appends to that array (this is exactly what
-- bootstrap_tenant_roles.py does). There is no INSERT-only path for the grant.
--
-- Run:
--   docker exec -i foundation-postgres psql -U postgres -d foundation \
--     < deploy/docker-compose/seed-workspace-authz.sql
-- ============================================================================

BEGIN;

-- 1) Catalog: register workspace.read / workspace.manage (LOW risk, no MFA).
--    Without these rows authz /check raises PERMISSION_UNKNOWN → HTTP error → 403.
INSERT INTO authz.permission_definition
  (id, service, resource_type, action, description, risk_level,
   requires_mfa, requires_4eyes, requires_pam_jit, requires_business_hours, is_system)
VALUES
  ('workspace.read', 'workspace', 'workspace', 'read',
   'List or retrieve workspaces and their files.',
   'LOW', false, false, false, false, true),
  ('workspace.manage', 'workspace', 'workspace', 'manage',
   'Create, update, or delete a workspace and manage its files (upload / delete).',
   'LOW', false, false, false, false, true)
ON CONFLICT (id) DO NOTHING;

-- 2) member role (both dev tenants): grant the two explicit tokens, same tier
--    as session.read / run.create. Append-only, guarded, idempotent.
--    (array_append + = ANY style matches authz migrations 0005/0008; the
--    permissions column is varchar[], so the @> text[] operator does not apply.)
UPDATE authz.role
   SET permissions = array_append(COALESCE(permissions, ARRAY[]::text[]), 'workspace.read')
 WHERE name = 'member'
   AND tenant_id IN ('tnt_01J5DEVXX00000000000000001','tnt_01J5DEVXX00000000000000002')
   AND deleted_at IS NULL
   AND NOT ('workspace.read' = ANY(COALESCE(permissions, ARRAY[]::text[])));

UPDATE authz.role
   SET permissions = array_append(COALESCE(permissions, ARRAY[]::text[]), 'workspace.manage')
 WHERE name = 'member'
   AND tenant_id IN ('tnt_01J5DEVXX00000000000000001','tnt_01J5DEVXX00000000000000002')
   AND deleted_at IS NULL
   AND NOT ('workspace.manage' = ANY(COALESCE(permissions, ARRAY[]::text[])));

-- 3) tenant_admin role (both dev tenants): grant workspace.* wildcard, matching
--    the existing session.* / run.* / app.* pattern. Append-only, guarded.
UPDATE authz.role
   SET permissions = array_append(COALESCE(permissions, ARRAY[]::text[]), 'workspace.*')
 WHERE name = 'tenant_admin'
   AND tenant_id IN ('tnt_01J5DEVXX00000000000000001','tnt_01J5DEVXX00000000000000002')
   AND deleted_at IS NULL
   AND NOT ('workspace.*' = ANY(COALESCE(permissions, ARRAY[]::text[])));

-- super_admin already carries the "*" wildcard — no change needed.

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────────
SELECT id, service, resource_type, action
  FROM authz.permission_definition
 WHERE service = 'workspace'
 ORDER BY id;

SELECT name, tenant_id,
       'workspace.read'   = ANY(permissions) AS has_ws_read,
       'workspace.manage' = ANY(permissions) AS has_ws_manage,
       'workspace.*'      = ANY(permissions) AS has_ws_wildcard
  FROM authz.role
 WHERE name IN ('member','tenant_admin')
   AND tenant_id IN ('tnt_01J5DEVXX00000000000000001','tnt_01J5DEVXX00000000000000002')
   AND deleted_at IS NULL
 ORDER BY tenant_id, name;
