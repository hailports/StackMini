import Foundation

// swiftc -o /tmp/redraft-tests contrib/clip/Pulse.swift contrib/clip/tests/redraft/main.swift && /tmp/redraft-tests
var failures = 0
var count = 0
func expect(_ ok: @autoclosure () -> Bool, _ what: String, line: Int = #line) {
    count += 1
    if !ok() { failures += 1; print("FAIL line \(line): \(what)") }
}

let helper: [String: Any] = ["status": "ok", "sentences": [
    ["text": "hey Priya -- yes, it is built in.", "kind": "paisty", "pinned": false],
    ["text": "17 dealers sit below $300k today.", "kind": "edited", "pinned": true],
    ["text": "I can do it Friday.", "kind": "user", "pinned": true],
]]

// Pre-pins come from the helper; the default Redraft sends them.
var pins = RedraftPins.from(json: helper, fieldReadable: true)
expect(pins.source == .listed, "listed")
expect(pins.pinned == ["17 dealers sit below $300k today.", "I can do it Friday."], "edits start pinned")
expect((pins.request(fresh: false)?["pins"] as? [String]) == pins.pinned, "Redraft sends the pins")
expect(pins.note == nil, "no note when the box is readable")

// Tap to pin / unpin.
pins.toggle(0)
expect(pins.pinned.first == "hey Priya -- yes, it is built in.", "tap pins an untouched sentence")
pins.toggle(1)
pins.toggle(2)
pins.toggle(0)
expect(pins.pinned.isEmpty, "everything unpinned")
expect((pins.request(fresh: false)?["pins"] as? [String]) == [], "an empty choice is still sent")
pins.toggle(99)
expect(pins.pinned.isEmpty, "out of range tap is ignored")

// Fresh redraft ignores every pin.
let all = RedraftPins.from(json: helper, fieldReadable: true)
expect((all.request(fresh: true)?["fresh"] as? Bool) == true, "fresh")
expect(all.request(fresh: true)?["pins"] == nil, "fresh sends no pins")

// Field not readable: today's behaviour, one short line.
let blind = RedraftPins.from(json: nil, fieldReadable: false)
expect(blind.source == .unreadable && blind.note == "Reply box read from the screen — redraft rereads the screen", "unreadable note")
expect(blind.request(fresh: false) == nil, "unreadable sends nothing")
expect(blind.rows.isEmpty, "no rows")

// Draft deleted: fresh, no pins.
let gone = RedraftPins.from(json: ["status": "gone", "sentences": [Any]()], fieldReadable: true)
expect(gone.source == .gone && (gone.request(fresh: false)?["pins"] as? [String]) == [], "gone")

// Helper failed: the draft step decides.
let unknown = RedraftPins.from(json: nil, fieldReadable: true)
expect(unknown.source == .unknown && unknown.request(fresh: false) == nil, "helper failure falls back")

// Labels.
expect(RedraftPins.label("short one.") == "short one.", "short label unchanged")
let long = RedraftPins.label("this sentence is far too long to fit on a single row of the little redraft menu.", limit: 30)
expect(long.count <= 30 && long.hasSuffix("…"), "long label trimmed: \(long)")
expect(RedraftPins.label("line\nbreak  here") == "line break here", "whitespace flattened")

// Many sentences: rows cap.
let many: [String: Any] = ["status": "ok", "sentences": (0..<30).map { ["text": "s\($0).", "pinned": false] as [String: Any] }]
expect(RedraftPins.from(json: many, fieldReadable: true).rows.count == RedraftPins.maxRows, "rows capped")

if failures > 0 {
    print("\(failures) of \(count) failed")
    exit(1)
}
print("\(count) passed")
