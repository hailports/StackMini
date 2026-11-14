import Cocoa
import ApplicationServices
import ScreenCaptureKit
import Vision
import os

func paisteCommand() -> String {
    if let set = ProcessInfo.processInfo.environment["PAISTE_CMD"], !set.isEmpty {
        return set
    }
    let bundled = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/paiste-run").path
    if FileManager.default.isExecutableFile(atPath: bundled) {
        return bundled
    }
    return "hailports"
}

/// One click on the menu-bar paperclip. The reply is pasted into the focused text field when that field can take it. Otherwise it stays on the clipboard. Nothing is sent.
final class PaisteApp: NSObject, NSApplicationDelegate {
    var item: NSStatusItem?
    var board: SetupBoard?
    var busy = false
    var lastGood = "{}"
    var lastGoodAt = Date.distantPast
    var lastPID: pid_t = 0
    var frontPID: pid_t = 0
    var pasteTarget: AXUIElement?
    var pastePID: pid_t = 0
    var pasteAt = Date.distantPast
    let mark = PaisteApp.paperclip()
    var spinTimer: Timer?
    var spinAngle: CGFloat = 0
    let log = Logger(subsystem: "com.hailports.paiste", category: "click")

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.isVisible = true
        let frame = item.button?.window?.frame ?? .zero
        try? "visible \(item.isVisible) x \(frame.origin.x) y \(frame.origin.y) w \(frame.width)\n".write(toFile: "/tmp/paisty-status.txt", atomically: true, encoding: .utf8)
        self.item = item
        let button = item.button
        button?.image = mark
        button?.imagePosition = .imageOnly
        button?.imageScaling = .scaleProportionallyDown
        button?.toolTip = "paisty"
        button?.target = self
        button?.action = #selector(click)
        button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        if let icon = Bundle.main.url(forResource: "paisty", withExtension: "icns").flatMap(NSImage.init(contentsOf:)) {
            NSApp.applicationIconImage = icon
        }
        let route = ProcessInfo.processInfo.environment["PAISTE_ROUTE"] ?? (NSHomeDirectory() + "/.config/paiste/route.yaml")
        if !FileManager.default.fileExists(atPath: route) {
            openSetup()
        }
        Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            self?.remember()
        }
    }

    func startSpin() {
        spinTimer?.invalidate()
        spinAngle = 0
        item?.button?.appearsDisabled = false
        item?.button?.image = wheel(0)
        item?.button?.toolTip = "paisty"
        spinTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.spinAngle += 30
            self.item?.button?.image = self.wheel(self.spinAngle)
        }
    }

    func stopSpin() {
        spinTimer?.invalidate()
        spinTimer = nil
    }

    func wheel(_ angle: CGFloat) -> NSImage {
        let side: CGFloat = 18
        let image = NSImage(size: NSSize(width: side, height: side))
        image.lockFocus()
        let path = NSBezierPath()
        path.appendArc(withCenter: NSPoint(x: side / 2, y: side / 2), radius: 6.5, startAngle: angle, endAngle: angle + 280)
        path.lineWidth = 2
        path.lineCapStyle = .round
        NSColor.labelColor.setStroke()
        path.stroke()
        image.unlockFocus()
        image.isTemplate = true
        return image
    }

    func promptTrust() {
        if AXIsProcessTrusted() { return }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options = [key: true] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
        if let url = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent") {
            NSWorkspace.shared.open(url)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            NSApp.setActivationPolicy(.accessory)
        }
    }

    func remember() {
        guard !busy, AXIsProcessTrusted() else { return }
        if let front = NSWorkspace.shared.frontmostApplication, !Self.skipsFront(front) {
            frontPID = front.processIdentifier
        }
        guard frontPID != 0 else { return }
        if let field = Self.focusedInput(pid: frontPID) {
            pasteTarget = field
            pastePID = frontPID
            pasteAt = Date()
        } else if pastePID != frontPID {
            pasteTarget = nil
            pastePID = 0
        }
        let scene = Self.scene(of: frontPID)
        if Self.usable(scene) {
            lastGood = scene
            lastGoodAt = Date()
            lastPID = frontPID
        }
    }

    @objc func openSetup() {
        if board == nil { board = SetupBoard() }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        board?.show()
    }

    @objc func click() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.option) == true {
            openSetup()
            return
        }
        if busy { return }
        if !AXIsProcessTrusted() {
            promptTrust()
            item?.button?.image = NSImage(systemSymbolName: "questionmark", accessibilityDescription: "Allow paisty in Accessibility")
            item?.button?.toolTip = "System Settings, search assistive, turn on paisty"
            log.error("untrusted")
            return
        }
        busy = true
        startSpin()
        let cached = lastGood
        let cachedAt = lastGoodAt
        let cachedPID = lastPID
        let pid = frontPID
        let field = (pastePID == pid && Date().timeIntervalSince(pasteAt) < 8) ? pasteTarget : nil
        DispatchQueue.global(qos: .userInitiated).async {
            var scene = pid == 0 ? "{}" : Self.scene(of: pid)
            if pid == cachedPID, Date().timeIntervalSince(cachedAt) < 8 {
                scene = Self.keepSelection(fresh: scene, cached: cached)
            }
            var text = ""
            var blocked = ""
            if Self.usable(scene) {
                let drafted = Self.draft(scene)
                text = drafted.reply
                blocked = drafted.blocked
            }
            var captureNote = ""
            var composer: CGPoint?
            if text.isEmpty && blocked != "license" {
                if !CGPreflightScreenCaptureAccess() {
                    captureNote = "need-screen-recording"
                } else {
                    let seen = Self.screenText(pid: pid)
                    captureNote = seen.isEmpty ? "screen-empty" : "screen-\(seen.count)"
                    if !seen.isEmpty, let raw = Self.screenScene(seen, pid: pid) {
                        let drafted = Self.draft(raw)
                        text = drafted.reply
                        blocked = drafted.blocked
                        if text.isEmpty { captureNote = "screen-unusable" }
                    }
                }
            }
            if !text.isEmpty && field == nil && pid != 0 && CGPreflightScreenCaptureAccess() {
                composer = Self.composerPoint(pid: pid)
            }
            DispatchQueue.main.async {
                self.stopSpin()
                if !text.isEmpty {
                    let board = NSPasteboard.general
                    board.clearContents()
                    board.setString(text, forType: .string)
                    var pasted = field != nil && Self.insert(text, into: field!)
                    if !pasted, let composer {
                        pasted = Self.clickPaste(pid: pid, at: composer)
                    }
                    let outcome = pasted ? "pasted" : "copied"
                    self.item?.button?.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: pasted ? "Pasted" : "Copied")
                    self.item?.button?.toolTip = pasted ? "Pasted" : "Copied"
                    self.item?.button?.appearsDisabled = false
                    self.log.error("\(outcome, privacy: .public) \(captureNote, privacy: .public)")
                    self.busy = false
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                        self.item?.button?.image = self.mark
                        self.item?.button?.toolTip = "paisty"
                    }
                    return
                } else {
                    if captureNote == "need-screen-recording" {
                        CGRequestScreenCaptureAccess()
                    }
                    let hint = blocked == "license" ? "Right-click and paste your key" : (captureNote == "need-screen-recording" ? "Allow paisty in Screen Recording" : "Nothing to copy")
                    self.item?.button?.image = NSImage(systemSymbolName: "questionmark", accessibilityDescription: hint)
                    self.item?.button?.toolTip = hint
                    self.item?.button?.appearsDisabled = false
                    self.log.error("empty \(captureNote, privacy: .public)")
                    self.busy = false
                    return
                }
            }
        }
    }

    static func skipsFront(_ app: NSRunningApplication) -> Bool {
        let name = (app.localizedName ?? "").lowercased()
        let id = (app.bundleIdentifier ?? "").lowercased()
        if id == "com.hailports.paiste" { return true }
        let keys = ["clippy", "maccy", "flycut", "copyclip", "pastebot"]
        if keys.contains(where: { name.contains($0) || id.contains($0) }) { return true }
        return name == "paste"
    }

    static func focusedInput(pid: pid_t) -> AXUIElement? {
        let app = AXUIElementCreateApplication(pid)
        var focused: CFTypeRef?
        guard AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &focused) == .success,
              let raw = focused else { return nil }
        let element = raw as! AXUIElement
        return isEditable(element) ? element : nil
    }

    static func isEditable(_ el: AXUIElement) -> Bool {
        let role = (str(el, kAXRoleAttribute) ?? "")
        let sub = (str(el, kAXSubroleAttribute) ?? "").lowercased()
        if role != "AXTextArea" && role != "AXTextField" { return false }
        if role.localizedCaseInsensitiveContains("secure") || sub.contains("search") || sub.contains("secure") {
            return false
        }
        var settable = DarwinBoolean(false)
        if AXUIElementIsAttributeSettable(el, kAXSelectedTextAttribute as CFString, &settable) == .success, settable.boolValue {
            return true
        }
        settable = DarwinBoolean(false)
        if AXUIElementIsAttributeSettable(el, kAXValueAttribute as CFString, &settable) == .success, settable.boolValue {
            return true
        }
        return false
    }

    static func insert(_ text: String, into el: AXUIElement) -> Bool {
        AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        if AXUIElementSetAttributeValue(el, kAXSelectedTextAttribute as CFString, text as CFString) == .success {
            return true
        }
        let existing = (str(el, kAXValueAttribute) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if existing.count > 1 { return false }
        return AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, text as CFString) == .success
    }

    static func composerPoint(pid: pid_t) -> CGPoint? {
        let wait = DispatchSemaphore(value: 0)
        var point: CGPoint?
        Task {
            point = await findComposer(pid: pid)
            wait.signal()
        }
        if wait.wait(timeout: .now() + 6) == .timedOut { return nil }
        return point
    }

    static func findComposer(pid: pid_t) async -> CGPoint? {
        guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true) else { return nil }
        let windows = content.windows.filter {
            $0.owningApplication?.processID == pid && $0.isOnScreen && $0.frame.width > 240 && $0.frame.height > 160
        }
        guard let window = windows.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }) else { return nil }
        guard let bounds = cgBounds(pid: pid, matching: window.frame) else { return nil }
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let config = SCStreamConfiguration()
        config.width = max(1, Int(window.frame.width * 2))
        config.height = max(1, Int(window.frame.height * 2))
        config.showsCursor = false
        config.capturesAudio = false
        guard let image = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) else { return nil }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        try? VNImageRequestHandler(cgImage: image).perform([request])
        var best: CGRect?
        for item in request.results ?? [] {
            guard let text = item.topCandidates(1).first?.string.lowercased() else { continue }
            guard isComposerLabel(text) else { continue }
            let box = item.boundingBox
            guard box.midY < 0.4 else { continue }
            if best == nil || box.midY < best!.midY { best = box }
        }
        guard let best else { return nil }
        return CGPoint(
            x: bounds.minX + best.midX * bounds.width,
            y: bounds.minY + (1 - best.midY) * bounds.height
        )
    }

    static func isComposerLabel(_ text: String) -> Bool {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let hints = [
            "write a message", "type a message", "type a new message", "type / for more",
            "message #", "send a message", "write a reply", "write an email",
            "start typing", "type here", "add a comment", "shift + return", "shift+return",
            "imessage", "text message", "compose email",
        ]
        if hints.contains(where: { line.contains($0) }) { return true }
        return line.count < 48 && line.hasPrefix("message ")
    }

    static func cgBounds(pid: pid_t, matching frame: CGRect) -> CGRect? {
        let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
        var best: CGRect?
        var score = CGFloat.greatestFiniteMagnitude
        func num(_ raw: [String: Any], _ key: String) -> CGFloat {
            if let value = raw[key] as? CGFloat { return value }
            if let value = raw[key] as? Double { return CGFloat(value) }
            if let value = raw[key] as? NSNumber { return CGFloat(truncating: value) }
            return 0
        }
        for row in info {
            let owner = (row[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? -1
            if owner != Int(pid) { continue }
            guard let raw = row[kCGWindowBounds as String] as? [String: Any] else { continue }
            let rect = CGRect(x: num(raw, "X"), y: num(raw, "Y"), width: num(raw, "Width"), height: num(raw, "Height"))
            if rect.width < 200 || rect.height < 160 { continue }
            let gap = abs(rect.width - frame.width) + abs(rect.height - frame.height)
            if gap < score {
                score = gap
                best = rect
            }
        }
        return best
    }

    static func clickPaste(pid: pid_t, at point: CGPoint) -> Bool {
        NSRunningApplication(processIdentifier: pid)?.activate(options: [.activateIgnoringOtherApps])
        Thread.sleep(forTimeInterval: 0.16)
        guard let source = CGEventSource(stateID: .combinedSessionState) else { return false }
        let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)
        let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)
        down?.post(tap: .cgSessionEventTap)
        up?.post(tap: .cgSessionEventTap)
        Thread.sleep(forTimeInterval: 0.08)
        let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: true)
        keyDown?.flags = .maskCommand
        let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: false)
        keyUp?.flags = .maskCommand
        keyDown?.post(tap: .cgSessionEventTap)
        keyUp?.post(tap: .cgSessionEventTap)
        return keyDown != nil
    }

    static func screenScene(_ text: String, pid: pid_t) -> String? {
        let name = NSRunningApplication(processIdentifier: pid)?.localizedName
            ?? NSWorkspace.shared.frontmostApplication?.localizedName
            ?? ""
        let body: [String: Any] = [
            "app": name,
            "title": name,
            "field_text": text,
            "field_role": "AXTextArea",
            "origin": "screen",
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func screenText(pid: pid_t) -> String {
        let wait = DispatchSemaphore(value: 0)
        var found = ""
        Task {
            found = await Self.grabWindow(pid: pid)
            wait.signal()
        }
        _ = wait.wait(timeout: .now() + 8)
        return found
    }

    static func grabWindow(pid: pid_t) async -> String {
        guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true) else { return "" }
        let windows = content.windows.filter {
            $0.owningApplication?.processID == pid && $0.isOnScreen && $0.frame.width > 240 && $0.frame.height > 160
        }
        guard let window = windows.max(by: { lhs, rhs in
            lhs.frame.width * lhs.frame.height < rhs.frame.width * rhs.frame.height
        }) else { return "" }
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let config = SCStreamConfiguration()
        config.width = max(1, Int(window.frame.width * 2))
        config.height = max(1, Int(window.frame.height * 2))
        config.showsCursor = false
        config.capturesAudio = false
        guard let image = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) else { return "" }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        let handler = VNImageRequestHandler(cgImage: image)
        try? handler.perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
    }

    static func clipPath() -> NSBezierPath {
        let path = NSBezierPath()
        path.lineWidth = 2.3
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
        path.move(to: NSPoint(x: 4.8, y: 1.4))
        path.line(to: NSPoint(x: 4.8, y: 10.2))
        path.curve(to: NSPoint(x: 15.4, y: 10.0),
                   controlPoint1: NSPoint(x: 4.8, y: 16.4),
                   controlPoint2: NSPoint(x: 16.2, y: 16.2))
        path.curve(to: NSPoint(x: 8.6, y: 8.6),
                   controlPoint1: NSPoint(x: 14.6, y: 4.6),
                   controlPoint2: NSPoint(x: 8.4, y: 5.0))
        path.curve(to: NSPoint(x: 11.6, y: 11.8),
                   controlPoint1: NSPoint(x: 8.8, y: 11.2),
                   controlPoint2: NSPoint(x: 10.2, y: 12.6))
        return path
    }

    static func paperclip() -> NSImage {
        let side: CGFloat = 18
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            let path = clipPath()
            NSColor.black.setStroke()
            path.stroke()
            return true
        }
        image.isTemplate = true
        return image
    }

    static func logo(_ side: CGFloat) -> NSImage {
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            let path = clipPath()
            let bounds = path.bounds.insetBy(dx: -1.4, dy: -1.4)
            let fit = min(side / bounds.width, side / bounds.height) * 0.92
            var t = AffineTransform()
            t.translate(x: (side - bounds.width * fit) / 2 - bounds.minX * fit,
                        y: (side - bounds.height * fit) / 2 - bounds.minY * fit)
            t.scale(fit)
            let weight = path.lineWidth
            path.transform(using: t)
            path.lineWidth = weight * fit
            NSColor.black.setStroke()
            path.stroke()
            return true
        }
        return image
    }

    static func draft(_ scene: String) -> (reply: String, blocked: String) {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paiste-scene.json")
        try? scene.data(using: .utf8)?.write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        let task = Process()
        let script = paisteCommand()
        if script == "hailports" {
            task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            task.arguments = ["hailports", "paiste", "draft", "--scene", url.path]
        } else {
            task.executableURL = URL(fileURLWithPath: script)
            task.arguments = ["draft", "--scene", url.path]
        }
        let out = Pipe()
        let err = Pipe()
        task.standardOutput = out
        task.standardError = err
        do { try task.run() } catch { return ("", "") }
        task.waitUntilExit()
        _ = err.fileHandleForReading.readDataToEndOfFile()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let text = (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if text == "license" { return ("", "license") }
        return task.terminationStatus == 0 ? (text, "") : ("", "")
    }

    static func usable(_ scene: String) -> Bool {
        guard scene != "{}",
              let data = scene.data(using: .utf8),
              let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        let role = (doc["field_role"] as? String ?? "").lowercased()
        if role.contains("secure") || role.contains("password") { return false }
        if !(doc["selection"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
        if !(doc["field_text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
        let blocks = doc["blocks"] as? [[String: Any]] ?? []
        return blocks.contains {
            !(($0["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    static func scene(of pid: pid_t) -> String {
        let app = AXUIElementCreateApplication(pid)
        var focused: CFTypeRef?
        guard AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &focused) == .success,
              let element = focused else { return "{}" }
        return payload(element as! AXUIElement, appName: NSRunningApplication(processIdentifier: pid)?.localizedName ?? "")
    }

    static func payload(_ el: AXUIElement, appName: String) -> String {
        let role = str(el, kAXRoleAttribute) ?? ""
        let value = str(el, kAXValueAttribute) ?? ""
        let selected = str(el, kAXSelectedTextAttribute) ?? ""
        var window: CFTypeRef?
        AXUIElementCopyAttributeValue(el, kAXWindowAttribute as CFString, &window)
        let title = window != nil ? (str(window as! AXUIElement, kAXTitleAttribute) ?? "") : ""
        let body: [String: Any] = [
            "app": appName,
            "title": title,
            "field_text": value,
            "field_role": role,
            "selection": selected,
            "origin": "cocoa",
            "field_box": box(el),
            "blocks": blocks(near: el),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: body),
              let text = String(data: data, encoding: .utf8) else { return "{}" }
        return text
    }

    static func str(_ el: AXUIElement, _ attr: String) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &value) == .success else { return nil }
        return value as? String
    }

    static func box(_ el: AXUIElement) -> [String: Double] {
        var pos = CGPoint.zero
        var size = CGSize.zero
        var position: CFTypeRef?
        var dimensions: CFTypeRef?
        if AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &position) == .success, let position {
            AXValueGetValue(position as! AXValue, .cgPoint, &pos)
        }
        if AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &dimensions) == .success, let dimensions {
            AXValueGetValue(dimensions as! AXValue, .cgSize, &size)
        }
        return ["x": pos.x, "y": pos.y, "w": size.width, "h": size.height]
    }

    static func chrome(_ text: String) -> Bool {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if body.isEmpty { return true }
        if body.count > 160 { return false }
        let low = body.lowercased()
        return low.contains("ctrl+") || low.contains("cmd+") || low.contains("shortcut") || low.contains("type / for more")
    }

    static func keepSelection(fresh: String, cached: String) -> String {
        guard let freshData = fresh.data(using: .utf8),
              var doc = try? JSONSerialization.jsonObject(with: freshData) as? [String: Any],
              let cachedData = cached.data(using: .utf8),
              let old = try? JSONSerialization.jsonObject(with: cachedData) as? [String: Any] else { return fresh }
        let selected = (doc["selection"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let prior = (old["selection"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if selected.isEmpty, !prior.isEmpty, !chrome(prior) {
            doc["selection"] = prior
        }
        guard let data = try? JSONSerialization.data(withJSONObject: doc),
              let text = String(data: data, encoding: .utf8) else { return fresh }
        return text
    }

    static func blocks(near el: AXUIElement) -> [[String: Any]] {
        var parent: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, kAXWindowAttribute as CFString, &parent) == .success, let parent else { return [] }
        var rows: [[String: Any]] = []
        walk(parent as! AXUIElement, into: &rows, depth: 0)
        return rows
    }

    static func walk(_ el: AXUIElement, into rows: inout [[String: Any]], depth: Int) {
        if rows.count >= 80 || depth > 6 { return }
        let role = str(el, kAXRoleAttribute) ?? ""
        let low = role.lowercased()
        let skipTree = low.contains("toolbar") || low.contains("menu") || low.contains("popover")
        if !skipTree {
            let text = str(el, kAXValueAttribute) ?? str(el, kAXDescriptionAttribute) ?? ""
            let button = low.contains("button")
            if !text.isEmpty, !button, !chrome(text) {
                rows.append(["text": String(text.prefix(4000)), "role": role, "box": box(el)])
            }
        }
        if skipTree { return }
        var children: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &children) == .success,
              let list = children as? [AXUIElement] else { return }
        for child in list.prefix(40) {
            walk(child, into: &rows, depth: depth + 1)
            if rows.count >= 80 { return }
        }
    }
}

private let paisteDelegate = PaisteApp()

@main
enum PaisteMain {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.delegate = paisteDelegate
        app.run()
    }
}


