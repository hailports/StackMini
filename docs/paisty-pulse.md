# paisty "thinking" pulse — exact spec

Replaces the round spinner shown after a click on the paisty mark. The mark breathes and two soft
rings drift outward: calm, continuous, never a spinner (which reads as stuck or broken).
Source of truth: `contrib/clip/Pulse.swift` (`PulseSpec`), drawn in `PaisteMenu.swift`.

## Geometry (22 pt canvas, centre 11,11)
- Paperclip: the paisty mark, stroke 2.3, round caps/joins, centred on its path bounds.
  SVG (y down, 22 viewBox): `<g transform="translate(0.88 1.076)"><path d="M4.8 16.6 L4.8 7.8 C4.8 1.6 16.2 1.8 15.4 8 C14.6 13.4 8.4 13 8.6 9.4 C8.8 6.8 10.2 5.4 11.6 6.2"/></g>`
- Rings: circles centred at 11,11, stroke 1.2 (constant), radius 8.4 → 10.8 (just outside the mark, inside the canvas).

## Timing and easing
| part | value |
|---|---|
| breath period | **1.6 s**, loops forever |
| mark scale | 0.94 (t=0, the click) → **0.82** at 0.8 s → 0.94 at 1.6 s |
| mark opacity | 1.0 → **0.62** at 0.8 s → 1.0 |
| breath easing | sine in-out: `0.5 + 0.5·cos(2π·t/1.6)`; CSS `cubic-bezier(0.37, 0, 0.63, 1)` per half |
| rings | **2**, half a period apart (second ring delay −0.8 s), each lives 1.6 s |
| ring radius | 8.4 + 2.4 · easeOutCubic(p), `easeOutCubic(p) = 1 − (1−p)³` |
| ring opacity | `0.38 · min(1, p/0.2) · (1−p)^1.4` (born invisible, peak ≈ 0.28 at p = 0.2, fades to 0) |
| frame rate (app) | 30 fps |

## Colour
- App: template image, so it follows the menu bar (black on light, white on dark); rings and mark use the
  same ink with the alpha above. No colour accents: nothing red, nothing that could read as an error.
- Site: ink `#141414` on paper `#F3EFE6`; dark: ink `#FFFFFF` on `#1C1C1F`. Use `currentColor`.

## Reduce Motion (macOS setting / `prefers-reduced-motion: reduce`)
No scaling, no rings. Mark at scale 0.94, opacity breathes 1.0 → **0.55** → 1.0 over **2.4 s**, sine in-out.

## States around it (app)
idle (mark) → click → **thinking** (this pulse) → done (checkmark 1.2 s) → idle.
Failure shows a question mark with a hint. The "a reply may be needed" glow (dot + slow opacity, 1.6 s)
never runs while thinking.

## CSS (drop-in)
`.paisty-thinking` with `@keyframes paisty-breathe` and
`paisty-ring` (exact keyframes computed from the formulas above, stroke kept at 1.2 by dividing by scale).

## SwiftUI equivalent
```swift
struct PaistyThinking: View {
    @Environment(\.accessibilityReduceMotion) var reduce
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            let f = PulseSpec.frame(at: t, reduceMotion: reduce)   // from Pulse.swift
            ZStack {
                ForEach(0..<f.rings.count, id: \.self) { i in
                    Circle().stroke(lineWidth: f.rings[i].width)
                        .frame(width: f.rings[i].radius * 2, height: f.rings[i].radius * 2)
                        .opacity(f.rings[i].alpha)
                }
                PaperclipShape().stroke(style: StrokeStyle(lineWidth: 2.3, lineCap: .round, lineJoin: .round))
                    .frame(width: 18, height: 18).scaleEffect(f.scale).opacity(f.opacity)
            }.frame(width: 22, height: 22)
        }
    }
}
```

## Demo
The site version is the CSS in the `.paisty-thinking` block above; frames and GIFs are rendered from `PulseSpec.frame` at 10x.
