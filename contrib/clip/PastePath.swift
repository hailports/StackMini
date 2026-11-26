import Cocoa
import ApplicationServices

/// Paste-path helpers shared by the menu app and its harness test.
/// Nothing here presses Return or sends anything. Cmd-V only lands in the app
/// that owns the remembered field, and only when that app is frontmost.
enum PastePath {
    // MARK: debug log (~/.config/paiste/paste-debug.log, 0600, capped)

    static let logPath = NSHomeDirectory() + "/.config/paiste/paste-debug.log"
    static let logLock = NSLock()

    static func note(_ line: String) {
        logLock.lock()
        defer { logLock.unlock() }
        let fm = FileManager.default
        if let attrs = try? fm.attributesOfItem(atPath: logPath),
           let size = attrs[.size] as? NSNumber, size.intValue > 256_000 {
            try? fm.removeItem(atPath: logPath)
        }
        if !fm.fileExists(atPath: logPath) {
            fm.createFile(atPath: logPath, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let handle = FileHandle(forWritingAtPath: logPath) else { return }
        defer { handle.closeFile() }
        handle.seekToEndOfFile()
        let stamp = ISO8601DateFormatter().string(from: Date())
        if let data = (stamp + " " + line + "\n").data(using: .utf8) { handle.write(data) }
    }

    // MARK: AX reads

    static func str(_ el: AXUIElement, _ attr: String) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &value) == .success else { return nil }
        return value as? String
    }

    static func element(_ el: AXUIElement, _ attr: String) -> AXUIElement? {
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &raw) == .success, let raw,
              CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
        return (raw as! AXUIElement)
    }

    static func size(_ el: AXUIElement) -> CGSize {
        var raw: CFTypeRef?
        var out = CGSize.zero
        if AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &raw) == .success, let raw,
           CFGetTypeID(raw) == AXValueGetTypeID() {
            AXValueGetValue(raw as! AXValue, .cgSize, &out)
        }
        return out
    }

    /// Role, subrole, size, web flag. No field contents.
    static func describe(_ el: AXUIElement?) -> String {
        guard let el else { return "none" }
        let dims = size(el)
        return "\(str(el, kAXRoleAttribute) ?? "?")/\(str(el, kAXSubroleAttribute) ?? "-") \(Int(dims.width))x\(Int(dims.height)) web=\(isWebText(el))"
    }

    /// A WebKit/Electron contenteditable (New Outlook body, Gmail, Teams, Slack).
    /// Their AXSelectedText setter reports success but often inserts nothing.
    static func isWebText(_ el: AXUIElement) -> Bool {
        var names: CFArray?
        if AXUIElementCopyAttributeNames(el, &names) == .success, let list = names as? [String] {
            if list.contains("AXStartTextMarker") || list.contains("AXEditableAncestor") || list.contains("AXDOMIdentifier") {
                return true
            }
        }
        var node = el
        for _ in 0..<4 {
            guard let up = element(node, kAXParentAttribute) else { break }
            if str(up, kAXRoleAttribute) == "AXWebArea" { return true }
            node = up
        }
        return false
    }

    // MARK: verification

    static func squash(_ text: String) -> String {
        text.lowercased().unicodeScalars
            .filter { CharacterSet.alphanumerics.contains($0) }
            .map(String.init).joined()
    }

    /// Opening words, normalized. 60 characters, or the whole draft when it is shorter.
    static func probe(_ text: String) -> String {
        String(squash(text).prefix(60))
    }

    static func hits(_ probe: String, in el: AXUIElement) -> Int {
        guard !probe.isEmpty, let value = str(el, kAXValueAttribute) else { return 0 }
        return squash(value).components(separatedBy: probe).count - 1
    }

    // MARK: front app + keys

    /// The app that owns keyboard focus. AX is live from any thread; NSWorkspace is the fallback.
    static func frontPID() -> pid_t {
        let wide = AXUIElementCreateSystemWide()
        if let app = element(wide, kAXFocusedApplicationAttribute) {
            var owner: pid_t = 0
            if AXUIElementGetPid(app, &owner) == .success, owner != 0 { return owner }
        }
        return NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
    }

    /// A snapshot used when the value cannot be read as text.
    static func signature(_ el: AXUIElement) -> String {
        var parts: [String] = []
        for attr in [kAXValueAttribute, kAXNumberOfCharactersAttribute, kAXSelectedTextRangeAttribute] {
            var raw: CFTypeRef?
            if AXUIElementCopyAttributeValue(el, attr as CFString, &raw) == .success, let raw {
                parts.append(String(describing: raw))
            } else {
                parts.append("-")
            }
        }
        return parts.joined(separator: "|")
    }

    /// Bring the target app forward and wait until it really is frontmost.
    static func ensureFront(_ pid: pid_t) -> Bool {
        if frontPID() == pid { return true }
        guard let app = NSRunningApplication(processIdentifier: pid) else { return false }
        app.activate(options: [.activateIgnoringOtherApps])
        for _ in 0..<12 {
            Thread.sleep(forTimeInterval: 0.08)
            if frontPID() == pid { return true }
        }
        return frontPID() == pid
    }

    // MARK: same-app window guard

    /// The window that holds `el`: its AXWindow, else the nearest AXWindow ancestor.
    static func window(of el: AXUIElement) -> AXUIElement? {
        if let win = element(el, kAXWindowAttribute) { return win }
        var node: AXUIElement? = el
        for _ in 0..<40 {
            guard let cur = node else { break }
            if str(cur, kAXRoleAttribute) == (kAXWindowRole as String) { return cur }
            node = element(cur, kAXParentAttribute)
        }
        return nil
    }

    /// The window that gets keystrokes inside `pid`.
    static func focusedWindow(_ pid: pid_t) -> AXUIElement? {
        element(AXUIElementCreateApplication(pid), kAXFocusedWindowAttribute)
    }

    /// True when `pid`'s focused window is the one holding `el`, or when AX can't tell either window.
    static func onWindow(of el: AXUIElement, pid: pid_t) -> Bool {
        guard let win = window(of: el), let cur = focusedWindow(pid) else { return true }
        return CFEqual(win, cur)
    }

    /// Two compose windows in one app: the user clicked paisty in the first, then moved to the
    /// second while the draft was generating. Cmd-V follows keyboard focus, so it would land in
    /// the second window. Bring the remembered window back first, and report false when it would
    /// not come back, so the caller leaves the draft on the clipboard instead of pasting.
    static func refocusWindow(of el: AXUIElement, pid: pid_t) -> Bool {
        guard let win = window(of: el) else {
            note("  window unknown: no same-app guard")
            return true
        }
        guard let cur = focusedWindow(pid) else {
            note("  focused window unknown: no same-app guard")
            return true
        }
        if CFEqual(cur, win) { return true }
        note("  focus is on another window of the app; raising the remembered one")
        AXUIElementPerformAction(win, kAXRaiseAction as CFString)
        AXUIElementSetAttributeValue(win, kAXMainAttribute as CFString, kCFBooleanTrue)
        AXUIElementSetAttributeValue(AXUIElementCreateApplication(pid), kAXFocusedWindowAttribute as CFString, win)
        for _ in 0..<10 {
            Thread.sleep(forTimeInterval: 0.05)
            if let now = focusedWindow(pid), CFEqual(now, win) {
                note("  remembered window is focused again")
                return true
            }
        }
        note("  remembered window did not come back: no paste")
        return false
    }

    /// Cmd-V only. No Return, no other keys.
    static func commandV() -> Bool {
        guard let source = CGEventSource(stateID: .combinedSessionState),
              let down = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: false) else { return false }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cgSessionEventTap)
        Thread.sleep(forTimeInterval: 0.02)
        up.post(tap: .cgSessionEventTap)
        return true
    }

    /// Cmd-C only. No Return, no other keys.
    static func commandC() -> Bool {
        guard let source = CGEventSource(stateID: .combinedSessionState),
              let down = CGEvent(keyboardEventSource: source, virtualKey: 8, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 8, keyDown: false) else { return false }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cgSessionEventTap)
        Thread.sleep(forTimeInterval: 0.02)
        up.post(tap: .cgSessionEventTap)
        return true
    }

    /// Copy the front app's selection with Cmd-C, then put the user's clipboard back.
    /// Returns "" when nothing new was copied. Call off the main thread.
    static func copySelection(pid: pid_t) -> String {
        guard pid != 0, frontPID() == pid else { return "" }
        var snapshot: [[NSPasteboard.PasteboardType: Data]] = []
        var before = 0
        DispatchQueue.main.sync {
            let board = NSPasteboard.general
            before = board.changeCount
            for item in board.pasteboardItems ?? [] {
                var types: [NSPasteboard.PasteboardType: Data] = [:]
                for type in item.types {
                    if let data = item.data(forType: type) { types[type] = data }
                }
                snapshot.append(types)
            }
        }
        guard commandC() else { return "" }
        var changed = false
        for _ in 0..<12 {
            Thread.sleep(forTimeInterval: 0.05)
            var now = 0
            DispatchQueue.main.sync { now = NSPasteboard.general.changeCount }
            if now != before { changed = true; break }
        }
        var text = ""
        if changed {
            DispatchQueue.main.sync { text = NSPasteboard.general.string(forType: .string) ?? "" }
        }
        if changed {
            DispatchQueue.main.sync {
                let board = NSPasteboard.general
                board.clearContents()
                var items: [NSPasteboardItem] = []
                for types in snapshot {
                    let item = NSPasteboardItem()
                    for (type, data) in types { item.setData(data, forType: type) }
                    items.append(item)
                }
                if !items.isEmpty { board.writeObjects(items) }
            }
        }
        note("copy-selection pid=\(pid) chars=\(text.count) changed=\(changed)")
        return text
    }

    /// Place text in a field the user already had open. Returns the path that
    /// landed ("ax-insert", "ax-value", "cmdv"), "cmdv-unverified" when the
    /// keystroke went to the right app but AX could not confirm it, or nil when
    /// nothing was attempted that could have landed. The caller has already put
    /// the text on the clipboard.
    static func insert(_ text: String, into el: AXUIElement, pid: pid_t) -> String? {
        let role = str(el, kAXRoleAttribute) ?? ""
        let web = isWebText(el)
        let mark = probe(text)
        let before = hits(mark, in: el)
        note("insert target \(describe(el)) front=\(frontPID() == pid)")
        if !web {
            let readable = !mark.isEmpty && str(el, kAXValueAttribute) != nil
            let sigBefore = signature(el)
            AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
            let rc = AXUIElementSetAttributeValue(el, kAXSelectedTextAttribute as CFString, text as CFString)
            Thread.sleep(forTimeInterval: 0.12)
            if rc == .success && !readable {
                // Cannot read the text back. A changed field means it landed; an unknown one
                // must not get a second paste on top.
                let changed = signature(el) != sigBefore
                note("  ax-selectedtext rc=0 unreadable changed=\(changed)")
                return changed ? "ax-insert" : "ax-insert-unverified"
            }
            let landed = readable && hits(mark, in: el) > before
            note("  ax-selectedtext rc=\(rc.rawValue) landed=\(landed)")
            if landed { return "ax-insert" }
            if role == "AXTextArea" || role == "AXTextField" {
                let existing = (str(el, kAXValueAttribute) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if readable && existing.count <= 1 {
                    let vrc = AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, text as CFString)
                    Thread.sleep(forTimeInterval: 0.12)
                    let ok = hits(mark, in: el) > before
                    note("  ax-value rc=\(vrc.rawValue) landed=\(ok)")
                    if ok { return "ax-value" }
                }
            }
        } else {
            note("  web field: skip AX text set (no-op on WebKit), use Cmd-V")
        }
        // Last resort: Cmd-V into the remembered app, only when it is frontmost.
        guard ensureFront(pid) else {
            note("  cmdv skipped: target app not frontmost")
            return nil
        }
        guard refocusWindow(of: el, pid: pid) else { return nil }
        AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        Thread.sleep(forTimeInterval: 0.08)
        guard frontPID() == pid, onWindow(of: el, pid: pid), commandV() else {
            note("  cmdv skipped: focus moved")
            return nil
        }
        for _ in 0..<10 {
            Thread.sleep(forTimeInterval: 0.1)
            if hits(mark, in: el) > before {
                note("  cmdv landed")
                return "cmdv"
            }
        }
        note("  cmdv sent, not confirmed by AX")
        return "cmdv-unverified"
    }
}
