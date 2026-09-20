import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { palette } from './theme';

const ThemeContext = createContext(null);
const STORAGE_KEY = 'ledger-theme';

function getInitialMode() {
  if (typeof window === 'undefined') return 'light';
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function ThemeProvider({ children }) {
  const [mode, setMode] = useState(getInitialMode);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', mode === 'dark');
    document.documentElement.style.colorScheme = mode;
    try {
      window.localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      // localStorage unavailable — theme just won't persist
    }
  }, [mode]);

  const value = useMemo(
    () => ({
      mode,
      colors: palette[mode],
      toggle: () => setMode((m) => (m === 'light' ? 'dark' : 'light')),
      setMode,
    }),
    [mode]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider');
  return ctx;
}
