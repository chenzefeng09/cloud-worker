import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
/**
 * TODO: This page calls /admin/audit/events:query which proxies the foundation
 * audit service. The BFF endpoint `/admin/audit/events:query` may not yet be
 * implemented. When it is, remove the placeholder banner and this page will
 * start working automatically.
 */
export function AuditPage() {
    const { tenants, tenantsLoading, setTenants, setTenantsLoading } = useAdminStore();
    const [tenantId, setTenantId] = useState('');
    const [action, setAction] = useState('');
    const [from, setFrom] = useState('');
    const [to, setTo] = useState('');
    const [events, setEvents] = useState([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
    const [queried, setQueried] = useState(false);
    // Bootstrap tenant list for filter
    if (tenants.length === 0 && !tenantsLoading) {
        setTenantsLoading(true);
        api.admin.tenants
            .list()
            .then((data) => setTenants(data.items))
            .catch(() => { })
            .finally(() => setTenantsLoading(false));
    }
    const handleQuery = async (e) => {
        e.preventDefault();
        setLoading(true);
        setError(null);
        setIsTodoEndpoint(false);
        setQueried(true);
        try {
            const body = { limit: 50 };
            if (tenantId)
                body.tenant_id = tenantId;
            if (action)
                body.action = action;
            if (from)
                body.from = from;
            if (to)
                body.to = to;
            const data = await api.admin.audit.query(body);
            setEvents(data.items);
        }
        catch (err) {
            if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
                setIsTodoEndpoint(true);
                setEvents([]);
            }
            else {
                setError(err instanceof ApiError ? err.message : '查询失败');
            }
        }
        finally {
            setLoading(false);
        }
    };
    return (_jsxs("div", { className: "flex flex-col gap-5 p-6 overflow-auto h-full", children: [_jsxs("div", { children: [_jsx("h1", { className: "text-[18px] font-display text-ink tracking-[-0.02em]", children: "\u5BA1\u8BA1\u65E5\u5FD7" }), _jsx("p", { className: "text-[12px] text-dim mt-0.5", children: "\u67E5\u8BE2\u5E73\u53F0\u64CD\u4F5C\u4E8B\u4EF6" })] }), _jsxs("div", { className: "border border-warn/30 rounded-md px-4 py-3 bg-warn/5 text-[12px] text-warn font-mono", children: ["TODO: BFF \u7AEF\u70B9 ", _jsx("code", { className: "bg-warn/10 px-1 rounded-sm", children: "POST /admin/audit/events:query" }), " \u5C1A\u672A\u5728 BFF \u5C42\u66B4\u9732\u3002 UI \u5DF2\u5C31\u7EEA\uFF0C\u63A5\u7EBF\u5360\u4F4D\u3002\u5F85 BFF \u4EE3\u7406 audit \u670D\u52A1\u540E\uFF0C\u672C\u63D0\u793A\u53EF\u5220\u9664\u3002"] }), _jsxs("form", { onSubmit: handleQuery, className: "border border-line rounded-md p-4 bg-surface flex flex-col gap-3", children: [_jsx("div", { className: "font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u67E5\u8BE2\u6761\u4EF6" }), _jsxs("div", { className: "grid grid-cols-2 gap-3", children: [_jsxs("div", { className: "flex flex-col gap-1", children: [_jsx("label", { className: "text-[11px] font-mono text-dim", children: "\u79DF\u6237" }), _jsxs("select", { className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent", value: tenantId, onChange: (e) => setTenantId(e.target.value), disabled: tenantsLoading, children: [_jsx("option", { value: "", children: "\u5168\u90E8" }), tenants.map((t) => (_jsx("option", { value: t.id, children: t.name }, t.id)))] })] }), _jsxs("div", { className: "flex flex-col gap-1", children: [_jsx("label", { className: "text-[11px] font-mono text-dim", children: "\u64CD\u4F5C (action)" }), _jsx("input", { className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono", placeholder: "\u5982 tenant.create", value: action, onChange: (e) => setAction(e.target.value) })] }), _jsxs("div", { className: "flex flex-col gap-1", children: [_jsx("label", { className: "text-[11px] font-mono text-dim", children: "\u5F00\u59CB\u65F6\u95F4" }), _jsx("input", { type: "datetime-local", className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent", value: from, onChange: (e) => setFrom(e.target.value) })] }), _jsxs("div", { className: "flex flex-col gap-1", children: [_jsx("label", { className: "text-[11px] font-mono text-dim", children: "\u7ED3\u675F\u65F6\u95F4" }), _jsx("input", { type: "datetime-local", className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent", value: to, onChange: (e) => setTo(e.target.value) })] })] }), _jsx("div", { children: _jsx("button", { type: "submit", disabled: loading, className: "px-3 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50", children: loading ? '查询中…' : '查询' }) })] }), error && (_jsx("div", { className: "border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent", children: error })), isTodoEndpoint && (_jsx("div", { className: "border border-line rounded-md p-4 text-[12px] text-dim italic", children: "\u5BA1\u8BA1\u4EE3\u7406\u7AEF\u70B9\u5C1A\u672A\u5C31\u7EEA\u3002\u63A5\u7EBF\u540E\u5C06\u81EA\u52A8\u5C55\u793A\u67E5\u8BE2\u7ED3\u679C\u3002" })), !isTodoEndpoint && queried && !loading && (events.length === 0 ? (_jsx("div", { className: "font-mono text-[12px] text-dim italic", children: "\u65E0\u5339\u914D\u4E8B\u4EF6" })) : (_jsx("div", { className: "border border-line rounded-md overflow-hidden", children: _jsxs("table", { className: "w-full text-left border-collapse", children: [_jsx("thead", { className: "bg-paper-2 border-b border-line", children: _jsxs("tr", { children: [_jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u65F6\u95F4" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u64CD\u4F5C" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u8D44\u6E90" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "Actor" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u79DF\u6237" })] }) }), _jsx("tbody", { children: events.map((ev) => (_jsxs("tr", { className: "border-b border-line-2 hover:bg-paper-2 transition-colors", children: [_jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-dim whitespace-nowrap", children: new Date(ev.created_at).toLocaleString('zh-CN') }), _jsx("td", { className: "px-3 py-2 font-mono text-[12px] text-ink", children: ev.action }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-ink-2", children: ev.resource_type ? `${ev.resource_type}/${ev.resource_id ?? ''}` : '—' }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]", children: ev.actor_id ?? '—' }), _jsx("td", { className: "px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]", children: ev.tenant_id })] }, ev.id))) })] }) })))] }));
}
