import { create } from 'zustand';
export const useAdminStore = create((set) => ({
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
    upsertTenant: (tenant) => set((s) => {
        const idx = s.tenants.findIndex((t) => t.id === tenant.id);
        if (idx === -1) {
            return { tenants: [tenant, ...s.tenants] };
        }
        const updated = [...s.tenants];
        updated[idx] = tenant;
        return { tenants: updated };
    }),
}));
