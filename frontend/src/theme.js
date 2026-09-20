// Design tokens — single source of truth for colors used outside Tailwind's
// reach (Recharts renders raw SVG attributes, so it needs literal hex, not
// CSS custom properties). Keep this in sync with the `:root` / `.dark`
// variable blocks in index.css — same values, two representations.

export const palette = {
  light: {
    background: '#F8F8FC',
    surface: '#FFFFFF',
    surfaceMuted: '#F1F1F6',
    foreground: '#16161D',
    muted: '#6B7280',
    border: '#E7E7F0',
    primary: '#4F46E5',
    primarySoft: '#EEF0FC',
    secondaryForeground: '#3730A3',
    success: '#047857',
    successSoft: '#E3F9EE',
    destructive: '#DC2626',
    destructiveSoft: '#FDECEC',
    chart1: '#4F46E5',
    chart2: '#8B5CF6',
    chart3: '#06B6D4',
    chart4: '#F59E0B',
    chart5: '#F43F5E',
  },
  dark: {
    background: '#0A0A0F',
    surface: '#131319',
    surfaceMuted: '#1C1C25',
    foreground: '#F4F4F8',
    muted: '#9498A8',
    border: '#232330',
    primary: '#818CF8',
    primarySoft: '#1B1B2C',
    secondaryForeground: '#C7C9FF',
    success: '#34D399',
    successSoft: '#0E2A1E',
    destructive: '#FB7185',
    destructiveSoft: '#2B1518',
    chart1: '#818CF8',
    chart2: '#A78BFA',
    chart3: '#22D3EE',
    chart4: '#FBBF24',
    chart5: '#FB7185',
  },
};

export const chartPalette = (mode) => {
  const p = palette[mode];
  return [p.chart1, p.chart2, p.chart3, p.chart4, p.chart5];
};

export const tokens = {
  radius: '1rem',
  fontSans: "'IBM Plex Sans', ui-sans-serif, system-ui, sans-serif",
  fontMono: "'IBM Plex Mono', ui-monospace, monospace",
};
