import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { Fragment } from 'react';
import { useNavigate } from 'react-router-dom';
/**
 * Page header with breadcrumbs + optional right-side action slot. Mirrors
 * cloud-worker's `PageHead` but uses react-router for navigation and is
 * pure presentation so pages can drop it in without owning state.
 */
export function PageHead({ crumbs, status, actions }) {
    const navigate = useNavigate();
    return (_jsxs("div", { className: "flex items-center gap-3.5 px-6 py-3.5 border-b border-line bg-paper shrink-0", children: [_jsx("div", { className: "flex items-center gap-1.5 font-mono text-[11px] text-dim", children: crumbs.map((c, i) => (_jsxs(Fragment, { children: [i > 0 && _jsx("span", { className: "opacity-50", children: "/" }), c.to ? (_jsx("a", { onClick: () => navigate(c.to), className: "cursor-pointer text-dim hover:text-ink", children: c.label })) : (_jsx("b", { className: "text-ink font-semibold", children: c.label }))] }, i))) }), status, _jsx("div", { className: "flex-1" }), _jsx("div", { className: "flex items-center gap-1.5", children: actions })] }));
}
