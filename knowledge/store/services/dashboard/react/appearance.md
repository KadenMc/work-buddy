---
name: React Dashboard Appearance System
kind: concept
description: Semantic visual grammar, skins, color scheme, typography tiers, density, and accessibility contracts.
summary: Dashboard Core owns a token-complete visual grammar; widgets consume semantic tokens and standardized primitives so skins, accessibility preferences, and contributed UI remain coherent.
tags:
- dashboard
- react
- appearance
- theming
- skins
- accessibility
- typography
aliases:
- dashboard theme
- dashboard skin
- UI foundations
- visual system
parents:
- services/dashboard/react
entry_points:
- dashboard-react/src/theme
- dashboard-react/src/ui
dev_notes: |-
  Theme providers install semantic custom properties before React renders to avoid a scheme/skin flash. Device-local appearance preferences are appropriate for presentation settings that do not change shared Work Buddy meaning.

  Shared primitives are the implementation boundary for fields, buttons, overlays, focus treatment, and state feedback. Contributions should not depend on raw skin palette values or inject unrestricted global CSS.
---

Dashboard Core owns the visual grammar. A view or widget may choose composition and emphasis, but it must not invent an isolated theme that becomes discordant beside other contributions.

## Scheme and skin

Color scheme and skin are orthogonal:

- **scheme** selects light, dark, or System behavior;
- **skin** selects a token-complete visual character, including the orange-accented Default skin.

Changing either axis re-resolves semantic tokens. Widgets never assume that one accent, surface, or text color is present.

## UI foundations

Foundations define semantic color roles, spacing, radii, elevation, typography, focus, disabled states, selection, warnings, errors, and interaction feedback. Shared React primitives consume those foundations and are the compatibility boundary for built-in and contributed UI.

Contributions use semantic tokens and supported primitives. Raw palette values, host-view styling, unrestricted global CSS, and fixed light-only or dark-only assumptions are not shareable contribution contracts.

## Typography and density

Typography uses named scale tiers instead of unconstrained per-widget pixel sizes. Density is separate from font scale so a compact layout cannot make text unreadable.

## Accessibility requirements

Every complete skin supports:

- sufficient contrast in light and dark schemes;
- visible keyboard focus and hover/pressed/selected states;
- forced-colors behavior;
- reduced-motion behavior;
- readable typography at every supported scale;
- state communication that does not rely on color alone; and
- overlay placement that does not trap or obscure keyboard users.

Appearance preferences that only affect the device are stored locally. Settings that change shared domain meaning use their owning App or System authority instead.
