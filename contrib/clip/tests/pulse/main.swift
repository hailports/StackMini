import Foundation

// swiftc -o /tmp/pulse-tests contrib/clip/Pulse.swift contrib/clip/tests/pulse/main.swift && /tmp/pulse-tests
var failures = 0
var count = 0
func expect(_ ok: @autoclosure () -> Bool, _ what: String, line: Int = #line) {
    count += 1
    if !ok() { failures += 1; print("FAIL line \(line): \(what)") }
}

// Animation state machine
var m = PulseMachine()
expect(m.phase == .idle, "starts idle")
expect(m.send(.hintOn) == .hint, "idle + hint -> hint")
expect(m.send(.start) == .thinking, "click from hint -> thinking")
expect(m.send(.hintOn) == .thinking, "hint never interrupts thinking")
expect(m.send(.settle) == .thinking, "settle does not end thinking")
expect(m.animates, "thinking animates")
expect(m.send(.finished) == .done, "thinking + finished -> done")
expect(!m.animates, "done is still")
expect(m.send(.settle) == .idle, "done settles to idle")
_ = m.send(.start)
expect(m.send(.failed) == .failed, "thinking + failed -> failed")
expect(m.send(.finished) == .failed, "finished after failed is ignored")
expect(m.send(.start) == .thinking, "a new click from failed thinks again")
var n = PulseMachine()
expect(n.send(.finished) == .idle, "finished while idle is ignored")
expect(n.send(.hintOff) == .idle, "hintOff while idle stays idle")

// Pulse frames: a breath, never a spinner
let spec = PulseSpec.self
let f0 = spec.frame(at: 0, reduceMotion: false)
let fHalf = spec.frame(at: spec.period / 2, reduceMotion: false)
expect(abs(f0.scale - spec.maxScale) < 0.0001 && abs(f0.opacity - 1) < 0.0001, "starts at full size: no jump at the click")
expect(abs(fHalf.scale - spec.minScale) < 0.0001 && abs(fHalf.opacity - spec.minOpacity) < 0.0001, "breathed in at half period")
expect(spec.ringFrom * 2 >= 16.5 && spec.ringTo <= spec.canvas / 2, "rings sit outside the paperclip and inside the canvas")
let fEnd = spec.frame(at: spec.period, reduceMotion: false)
expect(abs(fEnd.scale - f0.scale) < 0.0001, "loops seamlessly")
expect(f0.rings.count == spec.ringCount, "two soft rings")
expect(f0.rings[0].alpha < 0.001, "a newborn ring is invisible, so it never cuts the paperclip")
var smooth = true
var prev = spec.frame(at: 0, reduceMotion: false)
for i in 1...480 {
    let f = spec.frame(at: Double(i) / spec.fps, reduceMotion: false)
    if abs(f.scale - prev.scale) > 0.02 || abs(f.opacity - prev.opacity) > 0.05 { smooth = false }
    for r in f.rings where r.radius < spec.ringFrom - 0.001 || r.radius > spec.ringTo + 0.001 || r.alpha < 0 || r.alpha > spec.ringAlpha + 0.001 { smooth = false }
    prev = f
}
expect(smooth, "no jumps between frames at 30 fps and rings stay in bounds")
let calm = spec.frame(at: 0.7, reduceMotion: true)
expect(calm.rings.isEmpty && calm.scale == 1, "Reduce Motion: no rings, no scaling")
expect(calm.opacity >= spec.calmMinOpacity && calm.opacity <= 1, "Reduce Motion: opacity breath only")
expect(spec.period >= 1.2 && spec.period <= 2.4, "calm pace, not a frantic spin")

// Click rules: second click offers Redraft
let now = Date()
let win = "123|Outlook|RE: Goals"
let last = LastDraft(text: "hey Priya", at: now.addingTimeInterval(-60), window: win, path: "ax-insert")
expect(ClickRouter.decide(menuGesture: false, busy: false, last: nil, window: win, now: now) == .draft, "first click drafts")
expect(ClickRouter.decide(menuGesture: false, busy: false, last: last, window: win, now: now) == .offerRedraft, "second click offers redraft")
expect(ClickRouter.decide(menuGesture: false, busy: true, last: last, window: win, now: now) == .ignore, "click while thinking is ignored")
expect(ClickRouter.decide(menuGesture: true, busy: false, last: last, window: win, now: now) == .menu, "right click opens the menu")
expect(ClickRouter.decide(menuGesture: false, busy: false, last: last, window: "999|Slack|general", now: now) == .draft, "other window drafts fresh")
let stale = LastDraft(text: "x", at: now.addingTimeInterval(-ClickRouter.redraftWindow - 1), window: win, path: "cmdv")
expect(ClickRouter.decide(menuGesture: false, busy: false, last: stale, window: win, now: now) == .draft, "old draft drafts fresh")
var book: [String: LastDraft] = [:]
ClickRouter.remember(last, in: &book, now: now)
ClickRouter.remember(LastDraft(text: "b", at: now, window: "w2", path: "copied"), in: &book, now: now)
ClickRouter.remember(stale, in: &book, now: now)
expect(book["w2"] != nil && book[win] == nil, "per-window state: stale entries drop out")
expect(!LastDraft(text: "b", at: now, window: "w", path: "copied").placed, "clipboard-only is not placed")

// Replace versus append
expect(ReplaceMode.from(plan: ["mode": "replace", "start16": 3, "length16": 40], fieldReadable: true, previouslyPlaced: true) == .replace(start16: 3, length16: 40), "found draft is replaced")
expect(ReplaceMode.from(plan: ["mode": "insert"], fieldReadable: true, previouslyPlaced: true) == .insert, "draft gone from a readable box: insert")
expect(ReplaceMode.from(plan: ["mode": "insert"], fieldReadable: false, previouslyPlaced: true) == .clipboardOnly, "unreadable box with our text: never append")
expect(ReplaceMode.from(plan: ["mode": "insert"], fieldReadable: false, previouslyPlaced: false) == .insert, "nothing placed before: insert")
expect(ReplaceMode.from(plan: ["mode": "replace", "start16": 0, "length16": 0], fieldReadable: true, previouslyPlaced: true) == .insert, "empty range is not a replace")

print("\(count - failures)/\(count) passed")
exit(failures == 0 ? 0 : 1)
