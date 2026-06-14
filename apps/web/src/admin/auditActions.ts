// ─── Audit event-type catalog ──────────────────────────────────────────────────
//
// Canonical list of audit `event_type` values the platform emits, paired with
// i18n label keys and a grouping for the multi-select filter on the 审计日志 page.
// `value` is the wire identifier sent upstream as `event_types`.
// `labelKey` is looked up via t() at render time; group keys use `audit.group.*`.

export interface AuditActionOption {
  value: string;
  /** i18n key — use t(labelKey) at render time */
  labelKey: string;
  /** i18n key for the group header — use t(groupKey) at render time */
  groupKey: string;
}

export const AUDIT_ACTIONS: AuditActionOption[] = [
  // 身份认证与用户
  { value: 'foundation.identity.user.invited',      labelKey: 'audit.action.user.invited',           groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.user.updated',      labelKey: 'audit.action.user.updated',           groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.user.frozen',       labelKey: 'audit.action.user.frozen',            groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.user.unfrozen',     labelKey: 'audit.action.user.unfrozen',          groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.user.deactivated',  labelKey: 'audit.action.user.deactivated',       groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.user.login_success', labelKey: 'audit.action.user.login_success',    groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.password.changed',  labelKey: 'audit.action.password.changed',       groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.mfa.enrolled',      labelKey: 'audit.action.mfa.enrolled',           groupKey: 'audit.group.identity' },
  { value: 'foundation.identity.mfa.deleted',       labelKey: 'audit.action.mfa.deleted',            groupKey: 'audit.group.identity' },

  // SSO 与单点登录
  { value: 'foundation.identity.sso.created', labelKey: 'audit.action.sso.created', groupKey: 'audit.group.sso' },
  { value: 'foundation.identity.sso.updated', labelKey: 'audit.action.sso.updated', groupKey: 'audit.group.sso' },
  { value: 'foundation.identity.sso.deleted', labelKey: 'audit.action.sso.deleted', groupKey: 'audit.group.sso' },

  // 权限与角色
  { value: 'foundation.authz.role.created',    labelKey: 'audit.action.role.created',    groupKey: 'audit.group.authz' },
  { value: 'foundation.authz.role.updated',    labelKey: 'audit.action.role.updated',    groupKey: 'audit.group.authz' },
  { value: 'foundation.authz.role.deleted',    labelKey: 'audit.action.role.deleted',    groupKey: 'audit.group.authz' },
  { value: 'foundation.authz.binding.created', labelKey: 'audit.action.binding.created', groupKey: 'audit.group.authz' },
  { value: 'foundation.authz.binding.revoked', labelKey: 'audit.action.binding.revoked', groupKey: 'audit.group.authz' },

  // 组织结构
  { value: 'foundation.org.org_unit.created',          labelKey: 'audit.action.org_unit.created',          groupKey: 'audit.group.org' },
  { value: 'foundation.org.org_unit.updated',          labelKey: 'audit.action.org_unit.updated',          groupKey: 'audit.group.org' },
  { value: 'foundation.org.org_unit.deleted',          labelKey: 'audit.action.org_unit.deleted',          groupKey: 'audit.group.org' },
  { value: 'foundation.org.org_unit.moved',            labelKey: 'audit.action.org_unit.moved',            groupKey: 'audit.group.org' },
  { value: 'foundation.org.org_unit.archived',         labelKey: 'audit.action.org_unit.archived',         groupKey: 'audit.group.org' },
  { value: 'foundation.org.team.created',              labelKey: 'audit.action.team.created',              groupKey: 'audit.group.org' },
  { value: 'foundation.org.team.updated',              labelKey: 'audit.action.team.updated',              groupKey: 'audit.group.org' },
  { value: 'foundation.org.team.deleted',              labelKey: 'audit.action.team.deleted',              groupKey: 'audit.group.org' },
  { value: 'foundation.org.team.member_added',         labelKey: 'audit.action.team.member_added',         groupKey: 'audit.group.org' },
  { value: 'foundation.org.team.member_removed',       labelKey: 'audit.action.team.member_removed',       groupKey: 'audit.group.org' },
  { value: 'foundation.org.user.transferred',          labelKey: 'audit.action.user.transferred',          groupKey: 'audit.group.org' },
  { value: 'foundation.org.user.secondary_assigned',   labelKey: 'audit.action.user.secondary_assigned',   groupKey: 'audit.group.org' },

  // 密钥加密
  { value: 'crypto.kek.rotated', labelKey: 'audit.action.kek.rotated', groupKey: 'audit.group.crypto' },
];

/** Ordered unique list of groupKey values as they appear in AUDIT_ACTIONS. */
export const AUDIT_ACTION_GROUPS: string[] = AUDIT_ACTIONS.reduce<string[]>((groups, a) => {
  if (!groups.includes(a.groupKey)) groups.push(a.groupKey);
  return groups;
}, []);
