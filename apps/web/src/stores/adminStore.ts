import { create } from 'zustand';
import type { AdminUser, Tenant } from '../api/types';

interface AdminState {
  tenants: Tenant[];
  tenantsLoading: boolean;
  tenantsError: string | null;

  users: AdminUser[];
  usersLoading: boolean;
  usersError: string | null;
  selectedTenantId: string | null;

  setTenants: (tenants: Tenant[]) => void;
  setTenantsLoading: (v: boolean) => void;
  setTenantsError: (e: string | null) => void;

  setUsers: (users: AdminUser[]) => void;
  setUsersLoading: (v: boolean) => void;
  setUsersError: (e: string | null) => void;
  setSelectedTenantId: (id: string | null) => void;

  upsertTenant: (tenant: Tenant) => void;
}

export const useAdminStore = create<AdminState>((set) => ({
  tenants: [],
  tenantsLoading: false,
  tenantsError: null,

  users: [],
  usersLoading: false,
  usersError: null,
  selectedTenantId: null,

  setTenants: (tenants) => set({ tenants }),
  setTenantsLoading: (tenantsLoading) => set({ tenantsLoading }),
  setTenantsError: (tenantsError) => set({ tenantsError }),

  setUsers: (users) => set({ users }),
  setUsersLoading: (usersLoading) => set({ usersLoading }),
  setUsersError: (usersError) => set({ usersError }),
  setSelectedTenantId: (selectedTenantId) => set({ selectedTenantId }),

  upsertTenant: (tenant) =>
    set((s) => {
      const idx = s.tenants.findIndex((t) => t.id === tenant.id);
      if (idx === -1) {
        return { tenants: [tenant, ...s.tenants] };
      }
      const updated = [...s.tenants];
      updated[idx] = tenant;
      return { tenants: updated };
    }),
}));
