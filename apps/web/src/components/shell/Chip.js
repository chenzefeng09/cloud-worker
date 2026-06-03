import { jsx as _jsx } from "react/jsx-runtime";
export function Chip({ children, accent, dashed, onClick, className }) {
    const cls = ['cw-chip', className].filter(Boolean).join(' ');
    const dataAccent = accent ? '1' : undefined;
    const dataDashed = dashed ? '1' : undefined;
    if (onClick) {
        return (_jsx("button", { type: "button", className: cls, "data-accent": dataAccent, "data-dashed": dataDashed, onClick: onClick, children: children }));
    }
    return (_jsx("span", { className: cls, "data-accent": dataAccent, "data-dashed": dataDashed, children: children }));
}
