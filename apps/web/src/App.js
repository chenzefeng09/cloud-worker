import { jsx as _jsx } from "react/jsx-runtime";
import { BrowserRouter } from 'react-router-dom';
import { AuthGate } from './components/AuthGate';
export function App() {
    return (_jsx(BrowserRouter, { children: _jsx(AuthGate, {}) }));
}
