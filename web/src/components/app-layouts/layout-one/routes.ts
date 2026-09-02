import { FilmIcon, LayoutDashboardIcon, type LucideIcon, SettingsIcon } from 'lucide-react';

export type Route = {
  title: string;
  url: string;
  icon?: LucideIcon;
  isActive?: boolean;
  items?: {
    title: string;
    url: string;
  }[];
};

/**
 * The two gates, and nothing else. Stages 1-2 and 4-12 run unattended; a
 * person is only ever asked for the idea decision and the sign-off.
 */
export const ROUTES: Route[] = [
  {
    title: 'Gate 1 · Ideas',
    url: '/queue',
    icon: LayoutDashboardIcon,
  },
  {
    title: 'Gate 2 · Cuts',
    url: '/review',
    icon: FilmIcon,
  },
  {
    title: 'Settings',
    url: '/settings',
    icon: SettingsIcon,
  },
];
