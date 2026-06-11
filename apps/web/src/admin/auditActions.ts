// ─── Audit event-type catalog ──────────────────────────────────────────────────
//
// Canonical list of audit `event_type` values the platform emits, paired with
// 中文 labels and a grouping for the multi-select filter on the 审计日志 page.
// `value` is the wire identifier sent upstream as `event_types`.

export interface AuditActionOption {
  value: string;
  label: string;
  group: string;
}

export const AUDIT_ACTIONS: AuditActionOption[] = [
  // 身份认证与用户
  { value: 'foundation.identity.user.invited', label: '用户邀请', group: '身份认证与用户' },
  { value: 'foundation.identity.user.updated', label: '用户信息更新', group: '身份认证与用户' },
  { value: 'foundation.identity.user.frozen', label: '用户冻结', group: '身份认证与用户' },
  { value: 'foundation.identity.user.unfrozen', label: '用户解冻', group: '身份认证与用户' },
  { value: 'foundation.identity.user.deactivated', label: '用户停用', group: '身份认证与用户' },
  { value: 'foundation.identity.user.login_success', label: '用户登录成功', group: '身份认证与用户' },
  { value: 'foundation.identity.password.changed', label: '密码变更', group: '身份认证与用户' },
  { value: 'foundation.identity.mfa.enrolled', label: 'MFA 注册', group: '身份认证与用户' },
  { value: 'foundation.identity.mfa.deleted', label: 'MFA 删除', group: '身份认证与用户' },

  // SSO 与单点登录
  { value: 'foundation.identity.sso.created', label: 'SSO 配置创建', group: 'SSO 与单点登录' },
  { value: 'foundation.identity.sso.updated', label: 'SSO 配置更新', group: 'SSO 与单点登录' },
  { value: 'foundation.identity.sso.deleted', label: 'SSO 配置删除', group: 'SSO 与单点登录' },

  // 权限与角色
  { value: 'foundation.authz.role.created', label: '角色创建', group: '权限与角色' },
  { value: 'foundation.authz.role.updated', label: '角色更新', group: '权限与角色' },
  { value: 'foundation.authz.role.deleted', label: '角色删除', group: '权限与角色' },
  { value: 'foundation.authz.binding.created', label: '权限绑定创建', group: '权限与角色' },
  { value: 'foundation.authz.binding.revoked', label: '权限绑定撤销', group: '权限与角色' },

  // 组织结构
  { value: 'foundation.org.org_unit.created', label: '组织单元创建', group: '组织结构' },
  { value: 'foundation.org.org_unit.updated', label: '组织单元更新', group: '组织结构' },
  { value: 'foundation.org.org_unit.deleted', label: '组织单元删除', group: '组织结构' },
  { value: 'foundation.org.org_unit.moved', label: '组织单元移动', group: '组织结构' },
  { value: 'foundation.org.org_unit.archived', label: '组织单元归档', group: '组织结构' },
  { value: 'foundation.org.team.created', label: '团队创建', group: '组织结构' },
  { value: 'foundation.org.team.updated', label: '团队更新', group: '组织结构' },
  { value: 'foundation.org.team.deleted', label: '团队删除', group: '组织结构' },
  { value: 'foundation.org.team.member_added', label: '团队成员添加', group: '组织结构' },
  { value: 'foundation.org.team.member_removed', label: '团队成员移除', group: '组织结构' },
  { value: 'foundation.org.user.transferred', label: '用户部门转移', group: '组织结构' },
  { value: 'foundation.org.user.secondary_assigned', label: '用户次级部门分配', group: '组织结构' },

  // 密钥加密
  { value: 'crypto.kek.rotated', label: '密钥轮换', group: '密钥加密' },
];

/** Ordered list of group names as they appear in AUDIT_ACTIONS. */
export const AUDIT_ACTION_GROUPS: string[] = AUDIT_ACTIONS.reduce<string[]>((groups, a) => {
  if (!groups.includes(a.group)) groups.push(a.group);
  return groups;
}, []);
