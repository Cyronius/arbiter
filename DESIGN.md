---
name: Arbiter
description: A live board for shared workstation resources and coding sessions.
colors:
  primary: '#8bd5ec'
  on-primary: '#132839'
  canvas: '#131d2c'
  surface: '#1d2a3d'
  surface-alt: '#293c54'
  ink: '#edf4ff'
  muted: '#bfcee2'
  divider: '#3e536d'
  masthead: '#101a28'
  warning-surface: '#4a371f'
  warning-ink: '#ffe1a0'
typography:
  brand:
    fontFamily: Inter, Segoe UI, sans-serif
    fontSize: 1.35rem
    fontWeight: 750
  panel-heading:
    fontFamily: Inter, Segoe UI, sans-serif
    fontSize: 1.2rem
    lineHeight: 1.1
    letterSpacing: -.035em
  control:
    fontFamily: Inter, Segoe UI, sans-serif
    fontSize: .875rem
    fontWeight: 650
  report:
    fontFamily: Inter, Segoe UI, sans-serif
    fontSize: .875rem
  timestamp:
    fontFamily: IBM Plex Mono, ui-monospace, monospace
    fontSize: .79rem
rounded:
  panel: 12px
  control: 7.8px
  status: 3px
spacing:
  row-block: .5rem
  row-inline: .9rem
  column-gap: .6rem
  desktop-gutter: 1.5rem
  mobile-gutter: .75rem
components:
  button-primary:
    backgroundColor: '{colors.primary}'
    textColor: '{colors.on-primary}'
    typography: '{typography.control}'
    rounded: '{rounded.control}'
  search-field:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    rounded: '{rounded.control}'
  status:
    backgroundColor: '{colors.surface-alt}'
    textColor: '{colors.ink}'
    rounded: '{rounded.status}'
  status-warning:
    backgroundColor: '{colors.warning-surface}'
    textColor: '{colors.warning-ink}'
    rounded: '{rounded.status}'
  panel:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    rounded: '{rounded.panel}'
---

## Overview

The board opens to a flat session list in the dark Observatory palette. The selected tab names the full-width workspace. Sessions start with a compact toolbar for search, grouping, and messaging.

## Colors

Use the cyan accent for selected tabs and filters, primary actions, running state, and capacity meters. Pair amber warnings with explicit state text. The quiet divider token separates rows, while input borders mix muted ink and the panel surface for stronger contrast.

## Typography

Use the shared sans-serif stack for labels and reports. Reserve tabular monospaced figures for times, machine readings, and resource counts.

## Layout

Keep the title, connection indicator, scheduling budgets, and host readings in the masthead. At 67rem and below, place machine readings on the masthead’s second row. At 44rem and below, replace horizontal tabs with a native area selector and reflow session rows for touch.

## Components

- Flat list, Roster, and Flow use the same compact session rows. Roster groups by provider or job type. Flow groups by the work state.
- Quick filters and state badges share labels. A session can match both Resource wait and Needs approval.
- Begin the Sessions toolbar with search and align Grouping and the Message all icon to the right.
- Use icon buttons with accessible names and hover and focus labels for individual rows and dialogs. Keep conversation actions beside the dialog title.
- Keep durable controls and readings visible during board refreshes.
