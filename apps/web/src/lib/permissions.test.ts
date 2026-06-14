import { describe, expect, it } from 'vitest';
import { hasPermission, isPlatformAdmin, permissionCovers } from './permissions';

describe('permissionCovers', () => {
  it('global wildcard covers anything', () => {
    expect(permissionCovers('*', 'authz.role.manage')).toBe(true);
    expect(permissionCovers('*', 'anything')).toBe(true);
    expect(permissionCovers('*', '*')).toBe(true);
  });

  it('exact match covers itself', () => {
    expect(permissionCovers('authz.role.manage', 'authz.role.manage')).toBe(true);
    expect(permissionCovers('session.read', 'session.read')).toBe(true);
  });

  it('prefix wildcard covers nested tokens', () => {
    expect(permissionCovers('authz.*', 'authz.role.manage')).toBe(true);
    expect(permissionCovers('authz.*', 'authz.binding.manage')).toBe(true);
    expect(permissionCovers('session.*', 'session.read')).toBe(true);
    expect(permissionCovers('authz.role.*', 'authz.role.manage')).toBe(true);
  });

  it('prefix wildcard does NOT cover a bare single segment', () => {
    expect(permissionCovers('authz.*', 'authz')).toBe(false);
  });

  it('prefix wildcard does NOT cover a sibling namespace', () => {
    expect(permissionCovers('authz.*', 'session.read')).toBe(false);
    expect(permissionCovers('authz.role.*', 'authz.binding.manage')).toBe(false);
  });

  it('prefix wildcard covers a deeper wildcard required token', () => {
    expect(permissionCovers('authz.*', 'authz.role.*')).toBe(true);
  });

  it('non-matching exact tokens do not cover', () => {
    expect(permissionCovers('session.read', 'session.write')).toBe(false);
    expect(permissionCovers('authz.role.manage', 'authz.binding.manage')).toBe(false);
  });
});

describe('hasPermission', () => {
  it('returns false for undefined or empty perm sets', () => {
    expect(hasPermission(undefined, 'authz.role.manage')).toBe(false);
    expect(hasPermission([], 'authz.role.manage')).toBe(false);
  });

  it('matches when any held token covers the requirement', () => {
    const tenantAdmin = ['session.*', 'run.*', 'authz.role.manage', 'authz.binding.manage'];
    expect(hasPermission(tenantAdmin, 'authz.role.manage')).toBe(true);
    expect(hasPermission(tenantAdmin, 'session.read')).toBe(true);
    expect(hasPermission(tenantAdmin, 'tenant.manage')).toBe(false);
  });

  it('global wildcard satisfies any requirement', () => {
    expect(hasPermission(['*'], 'tenant.manage')).toBe(true);
  });
});

describe('isPlatformAdmin', () => {
  it('true for global wildcard', () => {
    expect(isPlatformAdmin(['*'])).toBe(true);
  });

  it('true for explicit platform.admin', () => {
    expect(isPlatformAdmin(['platform.admin'])).toBe(true);
    expect(isPlatformAdmin(['platform.*'])).toBe(true);
  });

  it('false for a tenant admin without platform scope', () => {
    expect(isPlatformAdmin(['authz.role.manage', 'session.*'])).toBe(false);
  });

  it('false for undefined / empty', () => {
    expect(isPlatformAdmin(undefined)).toBe(false);
    expect(isPlatformAdmin([])).toBe(false);
  });
});
