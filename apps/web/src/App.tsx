import { BrowserRouter } from 'react-router-dom';
import { AuthGate } from './components/AuthGate';

export function App() {
  return (
    <BrowserRouter>
      <AuthGate />
    </BrowserRouter>
  );
}
