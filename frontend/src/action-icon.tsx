import type {ButtonHTMLAttributes} from 'react';

const paths = {
  conversation: 'M4 4h16v12H9l-5 4z M8 8h8 M8 12h6',
  message: 'M3 5h18v14H3z M3 6l9 7 9-7',
  broadcast: 'M5 6h16v13H5z M5 7l8 6 8-6 M2 8v9',
  send: 'M3 10l18-7-7 18-3-8z M11 13l10-10',
  close: 'M6 6l12 12 M18 6L6 18',
  older: 'M12 19V5 M6 11l6-6 6 6',
  pause: 'M8 5v14 M16 5v14',
  resume: 'M8 5l11 7-11 7z',
  update: 'M19 11a7 7 0 1 0-2 5 M19 5v6h-6',
  release: 'M6 6h12v12H6z',
} as const;

export function ActionIcon({icon, label, className, type = 'button', ...button}: ButtonHTMLAttributes<HTMLButtonElement> & {
  icon: keyof typeof paths; label: string;
}) {
  return <button {...button} type={type} className={['icon-button', `icon-${icon}`, className].filter(Boolean).join(' ')}
    data-tooltip={label} aria-label={label}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={paths[icon]}/>
    </svg>
  </button>;
}
