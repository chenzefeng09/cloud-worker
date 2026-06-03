import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
const TIERS = ['FREE', 'PRO', 'BUSINESS', 'ENTERPRISE', 'DEDICATED'];
const STATUSES = ['ACTIVE', 'SUSPENDED', 'TERMINATING', 'TERMINATED'];
const STATUS_COLOR = {
    ACTIVE: 'text-ok',
    SUSPENDED: 'text-warn',
    TERMINATING: 'text-dim',
    TERMINATED: 'text-dim',
};
// ─── Create Tenant Form ───────────────────────────────────────────────────────
function CreateTenantForm({ onCreated }) {
    const [open, setOpen] = useState(false);
    const [name, setName] = useState('');
    const [tier, setTier] = useState('FREE');
    const [adminEmail, setAdminEmail] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const handleSubmit = async (e) => {
        e.preventDefault();
        if (!name.trim() || !adminEmail.trim())
            return;
        setLoading(true);
        setError(null);
        try {
            const body = { name: name.trim(), tier, admin_email: adminEmail.trim() };
            const created = await api.admin.tenants.create(body);
            onCreated(created);
            setName('');
            setAdminEmail('');
            setTier('FREE');
            setOpen(false);
        }
        catch (err) {
            setError(err instanceof ApiError ? err.message : '创建失败');
        }
        finally {
            setLoading(false);
        }
    };
    if (!open) {
        return (_jsx("button", { onClick: () => setOpen(true), className: "px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 transition-opacity", children: "+ \u65B0\u5EFA\u79DF\u6237" }));
    }
    return (_jsxs("form", { onSubmit: handleSubmit, className: "border border-line rounded-md p-4 bg-surface flex flex-col gap-3 max-w-md", children: [_jsx("div", { className: "font-mono text-[11px] uppercase tracking-[0.1em] text-dim", children: "\u65B0\u5EFA\u79DF\u6237" }), error && (_jsx("div", { className: "text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm", children: error })), _jsxs("div", { className: "flex flex-col gap-1", children: [_jsx("label", { className: "text-[11px] text-dim font-mono", children: "\u79DF\u6237\u540D\u79F0" }), _jsx("input", { className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent", value: name, onChange: (e) => setName(e.target.value), placeholder: "acme-corp", required: true })] }), _jsxs("div", { className: "flex flex-col gap-1", children: [_jsx("label", { className: "text-[11px] text-dim font-mono", children: "\u5957\u9910" }), _jsx("select", { className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent", value: tier, onChange: (e) => setTier(e.target.value), children: TIERS.map((t) => (_jsx("option", { value: t, children: t }, t))) })] }), _jsxs("div", { className: "flex flex-col gap-1", children: [_jsx("label", { className: "text-[11px] text-dim font-mono", children: "\u7BA1\u7406\u5458\u90AE\u7BB1" }), _jsx("input", { className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent", type: "email", value: adminEmail, onChange: (e) => setAdminEmail(e.target.value), placeholder: "admin@acme.co", required: true })] }), _jsxs("div", { className: "flex gap-2", children: [_jsx("button", { type: "submit", disabled: loading, className: "px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50", children: loading ? '创建中…' : '创建' }), _jsx("button", { type: "button", onClick: () => setOpen(false), className: "px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink", children: "\u53D6\u6D88" })] })] }));
}
// ─── Edit Tenant Inline ───────────────────────────────────────────────────────
function EditTenantRow({ tenant, onPatched }) {
    const [editing, setEditing] = useState(false);
    const [tier, setTier] = useState(tenant.tier);
    const [status, setStatus] = useState(tenant.status);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const handleSave = async () => {
        const body = {};
        if (tier !== tenant.tier)
            body.tier = tier;
        if (status !== tenant.status)
            body.status = status;
        if (!body.tier && !body.status) {
            setEditing(false);
            return;
        }
        setLoading(true);
        setError(null);
        try {
            const updated = await api.admin.tenants.patch(tenant.id, body);
            onPatched(updated);
            setEditing(false);
        }
        catch (err) {
            setError(err instanceof ApiError ? err.message : '更新失败');
        }
        finally {
            setLoading(false);
        }
    };
    if (!editing) {
        return (_jsxs("tr", { className: "border-b border-line-2 hover:bg-paper-2 transition-colors group", children: [_jsxs("td", { className: "px-3 py-2 font-mono text-[12px] text-dim truncate max-w-[120px]", children: [tenant.id.slice(0, 8), "\u2026"] }), _jsx("td", { className: "px-3 py-2 text-[13px] text-ink", children: tenant.name }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-ink-2", children: tenant.tier }), _jsx("td", { className: `px-3 py-2 font-mono text-[11px] ${STATUS_COLOR[tenant.status]}`, children: tenant.status }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-dim", children: new Date(tenant.created_at).toLocaleDateString('zh-CN') }), _jsx("td", { className: "px-3 py-2", children: _jsx("button", { onClick: () => setEditing(true), className: "opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity", children: "\u7F16\u8F91" }) })] }));
    }
    return (_jsxs("tr", { className: "border-b border-line bg-surface", children: [_jsxs("td", { className: "px-3 py-2 font-mono text-[12px] text-dim", children: [tenant.id.slice(0, 8), "\u2026"] }), _jsx("td", { className: "px-3 py-2 text-[13px] text-ink", children: tenant.name }), _jsx("td", { className: "px-3 py-2", children: _jsx("select", { className: "border border-line rounded-sm px-1.5 py-0.5 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono", value: tier, onChange: (e) => setTier(e.target.value), children: TIERS.map((t) => _jsx("option", { value: t, children: t }, t)) }) }), _jsx("td", { className: "px-3 py-2", children: _jsx("select", { className: "border border-line rounded-sm px-1.5 py-0.5 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono", value: status, onChange: (e) => setStatus(e.target.value), children: STATUSES.map((s) => _jsx("option", { value: s, children: s }, s)) }) }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-dim", children: new Date(tenant.created_at).toLocaleDateString('zh-CN') }), _jsxs("td", { className: "px-3 py-2 flex items-center gap-2", children: [error && _jsx("span", { className: "text-[11px] text-accent", children: error }), _jsx("button", { onClick: handleSave, disabled: loading, className: "text-[11px] font-mono text-ok hover:opacity-80 disabled:opacity-50", children: loading ? '…' : '保存' }), _jsx("button", { onClick: () => { setEditing(false); setTier(tenant.tier); setStatus(tenant.status); }, className: "text-[11px] font-mono text-dim hover:text-ink", children: "\u53D6\u6D88" })] })] }));
}
// ─── Page ─────────────────────────────────────────────────────────────────────
export function TenantsPage() {
    const { tenants, tenantsLoading, tenantsError, setTenants, setTenantsLoading, setTenantsError, upsertTenant } = useAdminStore();
    useEffect(() => {
        let cancelled = false;
        setTenantsLoading(true);
        setTenantsError(null);
        api.admin.tenants
            .list()
            .then((data) => { if (!cancelled)
            setTenants(data.items); })
            .catch((err) => { if (!cancelled)
            setTenantsError(err instanceof ApiError ? err.message : '加载失败'); })
            .finally(() => { if (!cancelled)
            setTenantsLoading(false); });
        return () => { cancelled = true; };
    }, [setTenants, setTenantsLoading, setTenantsError]);
    return (_jsxs("div", { className: "flex flex-col gap-5 p-6 overflow-auto h-full", children: [_jsxs("div", { className: "flex items-center justify-between", children: [_jsxs("div", { children: [_jsx("h1", { className: "text-[18px] font-display text-ink tracking-[-0.02em]", children: "\u79DF\u6237\u7BA1\u7406" }), _jsx("p", { className: "text-[12px] text-dim mt-0.5", children: "\u7BA1\u7406\u5E73\u53F0\u4E0A\u7684\u6240\u6709\u79DF\u6237" })] }), _jsx(CreateTenantForm, { onCreated: upsertTenant })] }), tenantsError && (_jsx("div", { className: "border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent", children: tenantsError })), tenantsLoading ? (_jsx("div", { className: "font-mono text-[12px] text-dim", children: "\u52A0\u8F7D\u4E2D\u2026" })) : tenants.length === 0 ? (_jsx("div", { className: "font-mono text-[12px] text-dim italic", children: "\u6682\u65E0\u79DF\u6237" })) : (_jsx("div", { className: "border border-line rounded-md overflow-hidden", children: _jsxs("table", { className: "w-full text-left border-collapse", children: [_jsx("thead", { className: "bg-paper-2 border-b border-line", children: _jsxs("tr", { children: [_jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "ID" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u540D\u79F0" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u5957\u9910" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u72B6\u6001" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u521B\u5EFA\u65F6\u95F4" }), _jsx("th", { className: "px-3 py-2 w-16" })] }) }), _jsx("tbody", { children: tenants.map((t) => (_jsx(EditTenantRow, { tenant: t, onPatched: upsertTenant }, t.id))) })] }) }))] }));
}
