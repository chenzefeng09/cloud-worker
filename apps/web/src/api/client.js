const BFF_BASE = '/api/bff/v1';
class ApiError extends Error {
    status;
    code;
    constructor(status, code, detail) {
        super(detail);
        this.status = status;
        this.code = code;
        this.name = 'ApiError';
    }
}
async function request(method, path, body) {
    const res = await fetch(`${BFF_BASE}${path}`, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        credentials: 'include',
    });
    if (!res.ok) {
        const problem = await res.json().catch(() => ({ detail: res.statusText }));
        throw new ApiError(res.status, problem.code || 'UNKNOWN', problem.detail || 'Request failed');
    }
    if (res.status === 204)
        return undefined;
    return res.json();
}
export const api = {
    auth: {
        login: (email, password) => request('POST', '/auth/login', { email, password }),
        logout: () => request('POST', '/auth/logout'),
        me: () => request('GET', '/auth/me'),
    },
    sessions: {
        list: (cursor) => request('GET', `/sessions?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
        create: (title) => request('POST', '/sessions', { title }),
        get: (id) => request('GET', `/sessions/${id}`),
        update: (id, data) => request('PATCH', `/sessions/${id}`, data),
        delete: (id) => request('DELETE', `/sessions/${id}`),
        cancel: (sessionId) => request('POST', `/sessions/${sessionId}/cancel`),
    },
    messages: {
        list: (sessionId, cursor) => request('GET', `/sessions/${sessionId}/messages${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
    },
    attachments: {
        upload: async (sessionId, file) => {
            const formData = new FormData();
            formData.append('file', file, file.name);
            const res = await fetch(`${BFF_BASE}/sessions/${sessionId}/attachments`, {
                method: 'POST',
                body: formData,
                credentials: 'include',
                // No Content-Type header — browser sets multipart boundary automatically
            });
            if (!res.ok) {
                const problem = await res.json().catch(() => ({ detail: res.statusText }));
                throw new ApiError(res.status, problem.code || 'UNKNOWN', problem.detail || 'Upload failed');
            }
            return res.json();
        },
        list: (sessionId, kind) => {
            const qs = kind ? `?kind=${encodeURIComponent(kind)}` : '';
            return request('GET', `/sessions/${sessionId}/attachments${qs}`);
        },
        getContent: async (sessionId, fileId) => {
            const res = await fetch(`${BFF_BASE}/sessions/${sessionId}/files/${fileId}/content`, {
                credentials: 'include',
            });
            if (!res.ok) {
                const problem = await res.json().catch(() => ({ detail: res.statusText }));
                throw new ApiError(res.status, problem.code || 'UNKNOWN', problem.detail || 'Fetch failed');
            }
            const mime = res.headers.get('content-type') || 'application/octet-stream';
            const blob = await res.blob();
            // For text-like content, also decode as string for preview
            const isText = mime.startsWith('text/') || mime.includes('json') || mime.includes('xml') || mime.includes('yaml') || mime.includes('markdown');
            let text = null;
            if (isText) {
                text = await blob.text();
            }
            return { text, blob, mime };
        },
    },
    library: {
        listMcpServers: () => request('GET', '/library/mcp-servers'),
        listSkills: () => request('GET', '/library/skills'),
        listDocuments: () => request('GET', '/library/documents'),
    },
    fleet: {
        poolSnapshot: () => request('GET', '/fleet/pool'),
        listSandboxes: () => request('GET', '/fleet/sandboxes'),
    },
    admin: {
        tenants: {
            list: () => request('GET', '/admin/tenants'),
            create: (body) => request('POST', '/admin/tenants', body),
            get: (tid) => request('GET', `/admin/tenants/${tid}`),
            patch: (tid, body) => request('PATCH', `/admin/tenants/${tid}`, body),
        },
        users: {
            list: (tenantId) => {
                const qs = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : '';
                return request('GET', `/admin/users${qs}`);
            },
        },
        // TODO: BFF proxy for audit events — POST /admin/audit/events:query → foundation audit
        audit: {
            query: (body) => request('POST', '/admin/audit/events:query', body),
        },
        // TODO: BFF proxy for billing — GET /admin/billing/quota?tenant_id=
        billing: {
            quota: (tenantId) => request('GET', `/admin/billing/quota?tenant_id=${encodeURIComponent(tenantId)}`),
        },
    },
    chat: {
        send: (sessionId, message, attachmentIds) => {
            const controller = new AbortController();
            fetch(`${BFF_BASE}/sessions/${sessionId}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message, attachment_ids: attachmentIds || [] }),
                credentials: 'include',
                signal: controller.signal,
            });
            return controller;
        },
    },
};
export { ApiError };
