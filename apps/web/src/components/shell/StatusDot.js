import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
const STATUS_MAP = {
    live: { c: '#d23a1f', anim: true, l: 'live' },
    thinking: { c: '#d23a1f', anim: true, l: 'thinking' },
    review: { c: '#c68a17', anim: false, l: 'review' },
    awaiting: { c: '#c68a17', anim: false, l: 'awaiting' },
    paused: { c: '#75756f', anim: false, l: 'paused' },
    done: { c: '#2c8f5d', anim: false, l: 'done' },
    queued: { c: '#75756f', anim: false, l: 'queued' },
    idle: { c: '#75756f', anim: false, l: 'idle' },
};
export function StatusDot({ status, label, }) {
    const s = STATUS_MAP[status] ?? STATUS_MAP.idle;
    const text = label ?? s.l;
    return (_jsxs("span", { style: { display: 'inline-flex', alignItems: 'center', gap: 6 }, children: [_jsx("span", { className: `cw-dot${s.anim ? ' animate-blip' : ''}`, style: { background: s.c } }), _jsx("span", { className: "cw-label", style: { fontSize: 11 }, children: text })] }));
}
export function sessionToDesignStatus(s) {
    switch (s) {
        case 'ACTIVE': return 'thinking';
        case 'COMPLETED': return 'done';
        case 'PAUSED': return 'paused';
        case 'ERROR': return 'awaiting';
        case 'ARCHIVED': return 'idle';
    }
}
