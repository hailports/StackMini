import Foundation
import CoreGraphics

// The "thinking" pulse and the click rules. Pure logic, no AppKit, so tests compile it alone
// (contrib/clip/tests/pulse/main.swift). The menu bar art in PaisteMenu.swift draws these frames.

/// What the menu bar mark is doing.
enum PulsePhase: String, Equatable {
    case idle       // plain paperclip
    case hint       // a reply may be needed: soft opacity glow with a dot
    case thinking   // drafting: the paperclip breathes and soft rings move outward
    case done       // checkmark for a moment
    case failed     // question mark with a hint until the next click
}

enum PulseEvent: Equatable {
    case hintOn, hintOff, start, finished, failed, settle
}

/// One owner for the mark's state, so a draft never shows a stale glow and a hint never
/// interrupts thinking.
struct PulseMachine: Equatable {
    private(set) var phase: PulsePhase = .idle

    @discardableResult
    mutating func send(_ event: PulseEvent) -> PulsePhase {
        switch (phase, event) {
        case (.idle, .hintOn): phase = .hint
        case (.hint, .hintOff): phase = .idle
        case (_, .start): phase = .thinking
        case (.thinking, .finished): phase = .done
        case (.thinking, .failed): phase = .failed
        case (.done, .settle): phase = .idle
        case (.failed, .settle): phase = .idle
        case (.failed, .hintOn): phase = .hint
        default: break // thinking ignores hints; settle while thinking does nothing
        }
        return phase
    }

    var animates: Bool { phase == .thinking || phase == .hint }
}

/// One frame of the thinking pulse, in a 22 pt square canvas.
struct PulseFrame: Equatable {
    var scale: CGFloat       // paperclip scale around the centre
    var opacity: CGFloat     // paperclip opacity
    var rings: [Ring]        // soft rings moving outward

    struct Ring: Equatable {
        var radius: CGFloat
        var alpha: CGFloat
        var width: CGFloat
    }
}

/// The exact animation. The site mirrors these numbers (see docs/paisty-pulse.md).
enum PulseSpec {
    static let canvas: CGFloat = 22
    static let period: Double = 1.6           // one breath, seconds
    static let minScale: CGFloat = 0.82
    static let maxScale: CGFloat = 0.94     // a touch under the idle mark: the click "breathes in"
    static let minOpacity: CGFloat = 0.62
    static let maxOpacity: CGFloat = 1.0
    static let ringCount = 2                  // half a period apart
    static let ringFrom: CGFloat = 8.4        // radius, pt: just outside the paperclip
    static let ringTo: CGFloat = 10.8
    static let ringAlpha: CGFloat = 0.38      // at birth; fades to 0
    static let ringWidth: CGFloat = 1.2
    static let ringFadeIn: Double = 0.2       // share of a ring's life spent fading in
    static let fps: Double = 30
    // Reduce Motion: no scaling and no rings, a slow opacity breath only.
    static let calmPeriod: Double = 2.4
    static let calmMinOpacity: CGFloat = 0.55

    /// Sine ease-in-out over one period: 1 at t=0 (the mark as it was at the click, no jump),
    /// 0 at half (breathed in), 1 again at the end.
    static func breath(_ t: Double, period: Double) -> CGFloat {
        let x = t.truncatingRemainder(dividingBy: period) / period
        return CGFloat(0.5 + 0.5 * cos(2 * Double.pi * x))
    }

    /// Ease-out for the rings: quick start, gentle finish.
    static func easeOut(_ x: Double) -> CGFloat {
        CGFloat(1 - pow(1 - min(max(x, 0), 1), 3))
    }

    static func frame(at t: Double, reduceMotion: Bool) -> PulseFrame {
        if reduceMotion {
            let b = breath(t, period: calmPeriod)
            return PulseFrame(scale: 1, opacity: calmMinOpacity + (maxOpacity - calmMinOpacity) * b, rings: [])
        }
        let b = breath(t, period: period)
        var rings: [PulseFrame.Ring] = []
        for i in 0..<ringCount {
            let offset = Double(i) / Double(ringCount)
            let p = (t / period + offset).truncatingRemainder(dividingBy: 1)
            let grow = easeOut(p)
            // Born invisible (no ring ever cuts through the paperclip), then fades out as it grows.
            let fade = CGFloat(min(1, p / ringFadeIn) * pow(1 - p, 1.4))
            rings.append(.init(radius: ringFrom + (ringTo - ringFrom) * grow, alpha: ringAlpha * fade, width: ringWidth))
        }
        return PulseFrame(scale: minScale + (maxScale - minScale) * b,
                          opacity: minOpacity + (maxOpacity - minOpacity) * b,
                          rings: rings)
    }
}

/// Paisty's last draft for one window.
struct LastDraft: Equatable {
    var text: String
    var at: Date
    var window: String
    var path: String     // how it landed: ax-insert, cmdv, copied, ...

    /// Did it go into the reply box, or only onto the clipboard?
    var placed: Bool { !path.hasPrefix("copied") }
}

enum ClickAction: Equatable {
    case menu           // right click or Option: the full menu
    case ignore         // already drafting
    case draft          // a fresh draft
    case offerRedraft   // a draft exists for this window: Redraft / Copy again / Cancel
}

enum ClickRouter {
    /// After this long a click drafts fresh instead of offering a redraft.
    static let redraftWindow: TimeInterval = 45 * 60

    static func decide(menuGesture: Bool, busy: Bool, last: LastDraft?, window: String, now: Date) -> ClickAction {
        if menuGesture { return .menu }
        if busy { return .ignore }
        guard let last, !window.isEmpty, last.window == window, !last.text.isEmpty,
              now.timeIntervalSince(last.at) >= 0, now.timeIntervalSince(last.at) < redraftWindow else {
            return .draft
        }
        return .offerRedraft
    }

    /// Per-window drafts, newest kept, old ones dropped.
    static func remember(_ draft: LastDraft, in book: inout [String: LastDraft], now: Date, keep: Int = 24) {
        book[draft.window] = draft
        book = book.filter { now.timeIntervalSince($0.value.at) < redraftWindow }
        if book.count > keep {
            let oldest = book.sorted { $0.value.at < $1.value.at }.prefix(book.count - keep).map(\.key)
            for key in oldest { book.removeValue(forKey: key) }
        }
    }
}

/// How the redraft lands, from the `locate` plan.
enum ReplaceMode: Equatable {
    case replace(start16: Int, length16: Int)
    case insert
    case clipboardOnly   // the old draft is there but cannot be selected safely: never append on top

    static func from(plan: [String: Any], fieldReadable: Bool, previouslyPlaced: Bool) -> ReplaceMode {
        if (plan["mode"] as? String) == "replace",
           let start = (plan["start16"] as? NSNumber)?.intValue,
           let length = (plan["length16"] as? NSNumber)?.intValue, length > 0, start >= 0 {
            return .replace(start16: start, length16: length)
        }
        // Can't read the box but paisty put text there before: a fresh insert would stack two replies.
        if !fieldReadable && previouslyPlaced { return .clipboardOnly }
        return .insert
    }
}

/// One sentence of paisty's earlier draft as it stands in the reply box, for the Redraft popup.
struct RedraftSentence: Equatable {
    var text: String
    var kind: String     // paisty, cosmetic, edited, user
    var pinned: Bool
}

/// Pin state for the Redraft popup. The bundled helper decides what starts pinned (the user's edits
/// and their own sentences); a tap toggles one. Pure logic so the tests compile it alone.
struct RedraftPins: Equatable {
    enum Source: Equatable {
        case unknown      // helper gave nothing: the draft step decides from the edits on its own
        case unreadable   // the reply box could not be read: today's redraft, no locks
        case gone         // the earlier draft was deleted: fresh
        case listed       // sentences listed with pins
    }

    var source: Source = .unknown
    var sentences: [RedraftSentence] = []

    static let unreadableNote = "Reply box read from the screen — redraft rereads the screen"
    static let maxRows = 12

    /// From the `locks` helper output, or from a field that could not be read.
    static func from(json: [String: Any]?, fieldReadable: Bool) -> RedraftPins {
        guard fieldReadable else { return RedraftPins(source: .unreadable) }
        guard let json else { return RedraftPins(source: .unknown) }
        let status = json["status"] as? String ?? ""
        if status == "gone" || status == "empty" { return RedraftPins(source: .gone) }
        let rows = (json["sentences"] as? [[String: Any]] ?? []).compactMap { row -> RedraftSentence? in
            guard let text = row["text"] as? String, !text.isEmpty else { return nil }
            let pinned = (row["pinned"] as? Bool) ?? ((row["pinned"] as? NSNumber)?.boolValue ?? false)
            return RedraftSentence(text: text, kind: row["kind"] as? String ?? "paisty", pinned: pinned)
        }
        return rows.isEmpty ? RedraftPins(source: .unknown) : RedraftPins(source: .listed, sentences: rows)
    }

    mutating func toggle(_ index: Int) {
        guard sentences.indices.contains(index) else { return }
        sentences[index].pinned.toggle()
    }

    var pinned: [String] { sentences.filter(\.pinned).map(\.text) }

    /// One short line under the header, or nil.
    var note: String? { source == .unreadable ? Self.unreadableNote : nil }

    /// Rows the popup shows; long drafts list the first few.
    var rows: ArraySlice<RedraftSentence> { sentences.prefix(Self.maxRows) }

    /// What the draft step gets. nil: no file, the draft step works it out from the field itself.
    /// A fresh redraft ignores every lock. A listed popup sends exactly the pins (an empty list keeps nothing).
    func request(fresh: Bool) -> [String: Any]? {
        if fresh { return ["fresh": true] }
        switch source {
        case .listed: return ["pins": pinned]
        case .gone: return ["pins": [String]()]
        case .unknown, .unreadable: return nil
        }
    }

    /// A sentence fits one menu row: trimmed near a word break to `limit` characters, with an ellipsis.
    static func label(_ text: String, limit: Int = 52) -> String {
        let flat = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard flat.count > limit else { return flat }
        let cut = flat.prefix(limit - 1)
        let word = cut.lastIndex(of: " ").map { cut[..<$0] } ?? cut
        let base = word.count >= limit / 2 ? word : cut
        return base.trimmingCharacters(in: CharacterSet(charactersIn: " ,;:-")) + "…"
    }
}
