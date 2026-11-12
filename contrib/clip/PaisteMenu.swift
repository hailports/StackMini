import Cocoa
import ApplicationServices
import ScreenCaptureKit
import Vision
import os

/// One click on the menu-bar paperclip. The reply is copied. Nothing is typed or sent.
final class PaisteApp: NSObject, NSApplicationDelegate {
    var item: NSStatusItem?
    var board: SetupBoard?
    var busy = false
    var lastGood = "{}"
    var lastGoodAt = Date.distantPast
    var lastPID: pid_t = 0
    var frontPID: pid_t = 0
    let mark = PaisteApp.paperclip()
    var spinTimer: Timer?
    var spinAngle: CGFloat = 0
    let log = Logger(subsystem: "com.hailports.paiste", category: "click")

    func applicationDidFinishLaunching(_ notification: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.item = item
        let button = item.button
        button?.image = mark
        button?.imagePosition = .imageOnly
        button?.imageScaling = .scaleProportionallyDown
        button?.toolTip = "paisty"
        button?.target = self
        button?.action = #selector(click)
        button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
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
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            NSApp.setActivationPolicy(.accessory)
        }
    }

    func remember() {
        guard AXIsProcessTrusted() else { return }
        if let front = NSWorkspace.shared.frontmostApplication,
           front.bundleIdentifier != "com.hailports.paiste" {
            frontPID = front.processIdentifier
        }
        guard frontPID != 0 else { return }
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
            item?.button?.toolTip = "Allow paisty in Accessibility"
            log.error("untrusted")
            return
        }
        busy = true
        startSpin()
        let cached = lastGood
        let cachedAt = lastGoodAt
        let cachedPID = lastPID
        let pid = frontPID
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
            let outcome = text.isEmpty ? "empty" : "copied"
            DispatchQueue.main.async {
                self.stopSpin()
                if !text.isEmpty {
                    let board = NSPasteboard.general
                    board.clearContents()
                    board.setString(text, forType: .string)
                    self.item?.button?.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: "Copied")
                    self.item?.button?.toolTip = "Copied"
                } else {
                    if captureNote == "need-screen-recording" {
                        CGRequestScreenCaptureAccess()
                    }
                    let hint = blocked == "license" ? "Right-click and paste your key" : (captureNote == "need-screen-recording" ? "Allow paisty in Screen Recording" : "Nothing to copy")
                    self.item?.button?.image = NSImage(systemSymbolName: "questionmark", accessibilityDescription: hint)
                    self.item?.button?.toolTip = hint
                    self.item?.button?.appearsDisabled = false
                    self.log.error("\(outcome, privacy: .public) \(captureNote, privacy: .public)")
                    self.busy = false
                    return
                }
                self.item?.button?.appearsDisabled = false
                self.log.error("\(outcome, privacy: .public) \(captureNote, privacy: .public)")
                self.busy = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                    self.item?.button?.image = self.mark
                    self.item?.button?.toolTip = "paisty"
                }
            }
        }
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

    static func paperclip() -> NSImage {
        let side: CGFloat = 18
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
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
            NSColor.black.setStroke()
            path.stroke()
            return true
        }
        image.isTemplate = true
        return image
    }

    static func draft(_ scene: String) -> (reply: String, blocked: String) {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paiste-scene.json")
        try? scene.data(using: .utf8)?.write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        let task = Process()
        let script = ProcessInfo.processInfo.environment["PAISTE_CMD"] ?? "hailports"
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
