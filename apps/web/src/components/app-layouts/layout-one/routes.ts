import { FilmIcon, LayoutDashboardIcon, type LucideIcon, ScissorsIcon, SettingsIcon } from 'lucide-react';

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
 * The gates, and nothing else. Stages 1-2 and 4-12 run unattended; a person is
 * only ever asked for a decision.
 *
 * Gate 3 is not on every path. It exists only when a recording has been
 * uploaded to clip, which is why it sits after the two that always apply rather
 * than before them in reading order — a clip's candidate becomes an idea that
 * is already approved, so it enters the pipeline past Gate 1.
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
    title: 'Gate 3 · Clips',
    url: '/clips',
    icon: ScissorsIcon,
  },
  {
    title: 'Settings',
    url: '/settings',
    icon: SettingsIcon,
  },
];
