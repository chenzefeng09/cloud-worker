import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
export function UsersPage() {
    const { tenants, tenantsLoading, setTenants, setTenantsLoading, users, usersLoading, usersError, setUsers, setUsersLoading, setUsersError, selectedTenantId, setSelectedTenantId, } = useAdminStore();
    // Bootstrap tenants list for the selector (may already be loaded)
    useEffect(() => {
        if (tenants.length > 0)
            return;
        setTenantsLoading(true);
        api.admin.tenants
            .list()
            .then((data) => setTenants(data.items))
            .catch(() => { })
            .finally(() => setTenantsLoading(false));
    }, [tenants.length, setTenants, setTenantsLoading]);
    // Load users whenever selection changes
    useEffect(() => {
        let cancelled = false;
        setUsersLoading(true);
        setUsersError(null);
        api.admin.users
            .list(selectedTenantId ?? undefined)
            .then((data) => { if (!cancelled)
            setUsers(data.items); })
            .catch((err) => { if (!cancelled)
            setUsersError(err instanceof ApiError ? err.message : '加载失败'); })
            .finally(() => { if (!cancelled)
            setUsersLoading(false); });
        return () => { cancelled = true; };
    }, [selectedTenantId, setUsers, setUsersLoading, setUsersError]);
    return (_jsxs("div", { className: "flex flex-col gap-5 p-6 overflow-auto h-full", children: [_jsxs("div", { children: [_jsx("h1", { className: "text-[18px] font-display text-ink tracking-[-0.02em]", children: "\u7528\u6237\u7BA1\u7406" }), _jsx("p", { className: "text-[12px] text-dim mt-0.5", children: "\u8DE8\u79DF\u6237\u7528\u6237\u67E5\u8BE2" })] }), _jsxs("div", { className: "flex items-center gap-3", children: [_jsx("label", { className: "text-[11px] font-mono text-dim uppercase tracking-[0.1em]", children: "\u7B5B\u9009\u79DF\u6237" }), _jsxs("select", { className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-w-[200px]", value: selectedTenantId ?? '', onChange: (e) => setSelectedTenantId(e.target.value || null), disabled: tenantsLoading, children: [_jsx("option", { value: "", children: "\u5168\u90E8\u79DF\u6237" }), tenants.map((t) => (_jsx("option", { value: t.id, children: t.name }, t.id)))] })] }), usersError && (_jsx("div", { className: "border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent", children: usersError })), usersLoading ? (_jsx("div", { className: "font-mono text-[12px] text-dim", children: "\u52A0\u8F7D\u4E2D\u2026" })) : users.length === 0 ? (_jsx("div", { className: "font-mono text-[12px] text-dim italic", children: "\u6682\u65E0\u7528\u6237" })) : (_jsx("div", { className: "border border-line rounded-md overflow-hidden", children: _jsxs("table", { className: "w-full text-left border-collapse", children: [_jsx("thead", { className: "bg-paper-2 border-b border-line", children: _jsxs("tr", { children: [_jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u90AE\u7BB1" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u663E\u793A\u540D" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u89D2\u8272" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u79DF\u6237 ID" })] }) }), _jsx("tbody", { children: users.map((u) => (_jsxs("tr", { className: "border-b border-line-2 hover:bg-paper-2 transition-colors", children: [_jsx("td", { className: "px-3 py-2 text-[13px] text-ink", children: u.email }), _jsx("td", { className: "px-3 py-2 text-[13px] text-ink-2", children: u.display_name || '—' }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-dim", children: u.roles.length > 0 ? u.roles.join(', ') : '—' }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[140px]", children: u.tenant_id })] }, u.id))) })] }) }))] }));
}
