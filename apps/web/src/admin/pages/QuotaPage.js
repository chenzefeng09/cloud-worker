import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
/**
 * TODO: This page calls /admin/billing/quota?tenant_id= which proxies to the
 * billing service. The BFF endpoint `/admin/billing/quota` may not yet be
 * implemented. When it is, remove the placeholder banner and this page will
 * start working automatically.
 */
export function QuotaPage() {
    const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } = useAdminStore();
    const [quota, setQuota] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
    // Bootstrap tenant list
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
    useEffect(() => {
        if (!selectedTenantId)
            return;
        let cancelled = false;
        setLoading(true);
        setError(null);
        setIsTodoEndpoint(false);
        api.admin.billing
            .quota(selectedTenantId)
            .then((data) => { if (!cancelled)
            setQuota(data); })
            .catch((err) => {
            if (cancelled)
                return;
            if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
                setIsTodoEndpoint(true);
            }
            else {
                setError(err instanceof ApiError ? err.message : '加载失败');
            }
        })
            .finally(() => { if (!cancelled)
            setLoading(false); });
        return () => { cancelled = true; };
    }, [selectedTenantId]);
    return (_jsxs("div", { className: "flex flex-col gap-5 p-6 overflow-auto h-full", children: [_jsxs("div", { children: [_jsx("h1", { className: "text-[18px] font-display text-ink tracking-[-0.02em]", children: "\u914D\u989D\u5957\u9910" }), _jsx("p", { className: "text-[12px] text-dim mt-0.5", children: "\u67E5\u770B\u79DF\u6237\u5F53\u524D\u7528\u91CF\u4E0E\u5957\u9910\u9650\u989D" })] }), _jsxs("div", { className: "border border-warn/30 rounded-md px-4 py-3 bg-warn/5 text-[12px] text-warn font-mono", children: ["TODO: BFF \u7AEF\u70B9 ", _jsx("code", { className: "bg-warn/10 px-1 rounded-sm", children: "/admin/billing/quota" }), " \u5C1A\u672A\u5728 BFF \u5C42\u66B4\u9732\u3002 \u8BE5\u9875\u9762 UI \u5DF2\u5C31\u7EEA\uFF0C\u63A5\u7EBF\u5360\u4F4D\u3002\u5F85 BFF \u4EE3\u7406 billing \u670D\u52A1\u540E\uFF0C\u672C\u63D0\u793A\u53EF\u5220\u9664\u3002"] }), _jsxs("div", { className: "flex items-center gap-3", children: [_jsx("label", { className: "text-[11px] font-mono text-dim uppercase tracking-[0.1em]", children: "\u9009\u62E9\u79DF\u6237" }), _jsxs("select", { className: "border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-w-[200px]", value: selectedTenantId ?? '', onChange: (e) => { setSelectedTenantId(e.target.value || null); setQuota(null); }, disabled: tenantsLoading, children: [_jsx("option", { value: "", children: "\u2014 \u8BF7\u9009\u62E9 \u2014" }), tenants.map((t) => (_jsx("option", { value: t.id, children: t.name }, t.id)))] })] }), error && (_jsx("div", { className: "border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent", children: error })), loading && _jsx("div", { className: "font-mono text-[12px] text-dim", children: "\u52A0\u8F7D\u4E2D\u2026" }), isTodoEndpoint && (_jsx("div", { className: "border border-line rounded-md p-4 text-[12px] text-dim italic", children: "BFF \u914D\u989D\u4EE3\u7406\u7AEF\u70B9\u5C1A\u672A\u5C31\u7EEA\uFF0C\u5F53\u524D\u5360\u4F4D\u663E\u793A\u3002\u63A5\u7EBF\u540E\u5C06\u81EA\u52A8\u5C55\u793A\u7528\u91CF\u6570\u636E\u3002" })), quota && (_jsxs("div", { className: "flex flex-col gap-4", children: [_jsxs("div", { className: "border border-line rounded-md p-4 bg-surface", children: [_jsx("div", { className: "font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim mb-2", children: "\u5F53\u524D\u5957\u9910" }), _jsx("div", { className: "text-[18px] font-display text-ink", children: quota.plan.name }), _jsx("div", { className: "font-mono text-[11px] text-dim mt-1", children: quota.plan.tier })] }), _jsxs("div", { className: "border border-line rounded-md overflow-hidden", children: [_jsx("div", { className: "bg-paper-2 px-3 py-2 border-b border-line font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim", children: "\u7528\u91CF / \u9650\u989D" }), _jsxs("table", { className: "w-full text-left border-collapse", children: [_jsx("thead", { className: "border-b border-line-2", children: _jsxs("tr", { children: [_jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] text-dim", children: "\u6307\u6807" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] text-dim", children: "\u5DF2\u7528" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] text-dim", children: "\u4E0A\u9650" }), _jsx("th", { className: "px-3 py-2 font-mono text-[10.5px] text-dim", children: "\u5360\u6BD4" })] }) }), _jsx("tbody", { children: Object.entries(quota.plan.limits).map(([key, limit]) => {
                                            const used = quota.usage[key] ?? 0;
                                            const pct = limit > 0 ? Math.round((used / limit) * 100) : 0;
                                            return (_jsxs("tr", { className: "border-b border-line-2 hover:bg-paper-2", children: [_jsx("td", { className: "px-3 py-2 font-mono text-[12px] text-ink-2", children: key }), _jsx("td", { className: "px-3 py-2 font-mono text-[12px] text-ink", children: used.toLocaleString() }), _jsx("td", { className: "px-3 py-2 font-mono text-[12px] text-dim", children: limit.toLocaleString() }), _jsx("td", { className: "px-3 py-2", children: _jsxs("div", { className: "flex items-center gap-2", children: [_jsx("div", { className: "flex-1 h-1.5 bg-line-2 rounded-full overflow-hidden max-w-[80px]", children: _jsx("div", { className: `h-full rounded-full ${pct >= 90 ? 'bg-accent' : pct >= 70 ? 'bg-warn' : 'bg-ok'}`, style: { width: `${Math.min(pct, 100)}%` } }) }), _jsxs("span", { className: "font-mono text-[11px] text-dim", children: [pct, "%"] })] }) })] }, key));
                                        }) })] })] })] })), !selectedTenantId && !loading && (_jsx("div", { className: "font-mono text-[12px] text-dim italic", children: "\u8BF7\u5148\u9009\u62E9\u4E00\u4E2A\u79DF\u6237" }))] }));
}
