# GisCode Design System — Ink & Ember

## Identity

GisCode's visual language is **Ink & Ember**: deep blue-black work surfaces, warm
amber for the next action, and restrained signal-teal for state, identity and
evidence. It reads as an engineering instrument, not a generic AI chat window.
The identity survives without the logo through the rail, typography, amber action
hierarchy and the “You / GisCode / Decision” conversation grammar.

## Marque and app icon

The marque is a broken engineering ring in amber with an inset teal code chevron
and a terminal block at the opening. It combines a compass (exploration) with a
prompt (code) and stays legible at 24 px.

Packaged assets:

- `giscode/assets/giscode_mark.svg`
- `giscode/assets/giscode_mark_{24,32,48,64,128,256,512}.png`
- `giscode/assets/giscode.ico`
- Generator: `build/identity/generate_marks.py`

The window icon uses the ICO; the command bar and About dialog use the raster
marque. The old GIS star remains bundled for older custom-logo flows, not as the
primary product mark.

## Colour

### Ink (dark, default)

| Role | Hex |
|---|---|
| Canvas | `#0A0E13` |
| Surface | `#101720` |
| Raised | `#172230` |
| Border | `#243244` |
| Text | `#E8EFF7` |
| Muted | `#9AB0C6` |
| Ember accent | `#F2A93B` |
| On accent | `#12161C` |
| Signal teal | `#4FD1C5` |
| Success | `#57D68D` |
| Warning / timeout | `#F6C45B` |
| Failure | `#FF7B72` |
| Cancelled | `#C4A7FF` |
| Info | `#79B8FF` |

### Paper (light)

| Role | Hex |
|---|---|
| Canvas | `#F4F1EA` |
| Surface | `#FFFFFF` |
| Raised | `#F8F5EE` |
| Border | `#D8D0C2` |
| Text | `#17212B` |
| Muted | `#566576` |
| Ember accent | `#B96D0B` |
| On accent | `#211200` |
| Signal teal | `#0F766E` |
| Success | `#1D7A46` |
| Warning / timeout | `#92610A` |
| Failure | `#B42318` |
| Cancelled | `#6B4BA8` |

**Rules**

- Amber marks the next action or an active attention state; never success.
- Success, warning, failure and cancellation are separate named colours.
- Colour is never the only carrier of meaning: every status has a label.
- Text/background pairs used by the UI meet WCAG AA (4.5:1 minimum). The test
  `tests/test_ui_identity.py` validates text, muted text, raised surfaces,
  question surfaces and accent-button text in both appearances.

## Typography

- UI: Segoe UI Variable Text → Segoe UI → Aptos → Tk default.
- Display: Segoe UI Variable Display → Segoe UI Semibold.
- Code/evidence: Cascadia Code → Cascadia Mono → Consolas.

Scale: 20/18 display and wordmark, 14 title, 10 body, 9 small, 8 eyebrow and
status. Eyebrows are uppercase and used sparingly for page and field context.
Body text is not condensed or decorative. Code and evidence use the mono face.

## Spacing, surfaces and elevation

- Spacing scale: 4, 8, 12, 18, 28, 40 px.
- Radius: 4 px controls, 8 px cards, 14 px panels (where the toolkit supports it).
- Surfaces: canvas → surface → raised → overlay, separated by a 1 px border.
- Elevation is a border and a surface step, not a heavy drop shadow. This keeps
  dense engineering pages legible and avoids animation or GPU cost.

## Application shell

- A 216 px **left rail** is the primary navigation: brand, Workspace / System /
  Tools groups, real shortcut hints (`Alt 1`…`Alt 9`), appearance toggle and a
  truthful `LOCAL · PRIVATE` footer.
- The command bar keeps the pinned project/mode/model controls and project
  actions. Amber is used for New Project and Send, not every button.
- The notebook remains the single authoritative page container and the tests’
  contract, but its tab strip is collapsed to zero geometry; the rail selects the
  same real pages. No duplicate navigation.
- Nested section tabs (Projects → Planer / Visual Design / Integrity) use the
  `Gis.SubTabs` style, remaining visible because they are local to that page.
- A 3 px top attention strip and a labelled status pill carry task state. The pill
  is the accessible source of truth; there is no flashing.
- The status bar states `LOCAL · PRIVATE` and the model state.

## Conversation grammar

| Role | Treatment |
|---|---|
| You | Amber `YOU` label and a tinted request surface. |
| GisCode | Teal `GISCODE` label on the reading surface. |
| GisCode needs a decision | Amber `GISCODE ASKS` label on a warm question surface. |
| Report | Mono evidence surface. |
| Tool activity | Muted italic treatment. |
| Code | Mono, code surface. |
| Failure | Failure colour, never success styling. |

## Controls and states

- **Buttons:** primary amber, secondary raised ink, ghost quiet, danger outlined
  red. Compact variants exist only for the Chat action row at the 1100 px floor.
- **Selectors:** project/mode/model and settings combos use the same dark field,
  border and amber focus; read-only and disabled states are distinct.
- **Inputs:** raised field, 1 px border, amber insertion caret and focus ring.
- **Tabs:** rail selected state uses a raised surface and 3 px amber indicator.
- **Tooltips:** delayed, keyboard-independent hints for icon/navigation controls.
- **Keyboard:** real `Alt 1`…`Alt 9` page shortcuts; Enter/Shift+Enter behaviour
  in the request editor is stated in the UI.
- **Attention:** stable labelled states `Ready`, `Working`, `Stopping`,
  `Completed`, `Cancelled`, `Timed out`, `Failed`; no animation. Users can turn
  the visual strip off while keeping the labelled pill.
- **Theme:** Ink / Paper switch live, is persisted as `ui.appearance`, and does
  not recreate pages, tasks or conversation state.

## Accessibility

- Both appearances pass the tested AA contrast pairs.
- Status is text + colour, not colour alone.
- Focus colour is amber with a visible border.
- The supported minimum window is 1100×700; all header controls and Chat
  Send/Stop were re-tested at that floor.
- Reduced motion is respected by having no ambient animation at all.

## Implementation map

- `giscode/gui/design_system.py` — tokens, ttk styles, contrast helpers, text
  tags, marque drawing, vector glyphs.
- `giscode/gui/shell.py` — navigation rail, nav items, tooltip and footer.
- `giscode/gui/main_window.py` — command bar, theme switch, attention states,
  Chat composition, Settings appearance and About.
- `giscode/gui/project_workspace.py` — nested Projects tab styling and page header.
