import { BrowserRouter } from 'react-router-dom';
import { AuthGate } from './components/AuthGate';
import { FeatureFlagProvider } from './hooks/useFeatureFlags';

export function App() {
  return (
    <BrowserRouter>
      <FeatureFlagProvider>
        <AuthGate />
      </FeatureFlagProvider>
    </BrowserRouter>
  );
}
