// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
//
// Frontend plugin registry — extension seam for proprietary overlays.
//
// In OSS this file is empty stubs. Downstream builds (e.g. the desktop
// installer) replace this file with one that imports proprietary pages and
// registers them. The OSS bundle ships unchanged with empty arrays.
//
// Contract: see docs/OVERLAY.md.
//
// Shape:
//   pluginRoutes: Array<{ path: string, element: React.ReactNode }>
//     Mounted inside the main <Route path="/" element={<Layout />}> tree.
//   pluginNavItems: Array<{ to, icon, label, position?: "top"|"bottom" }>
//     Spread into the sidebar in components/Layout.jsx. Default position: "top".

export const pluginRoutes = []
export const pluginNavItems = []
