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
    // Reply hint: a soft pulse when the front window likely needs a reply.
    var glowTimer: Timer?
    var glowStart = Date()
    var glowSig = ""
    var seenSig = ""
    var seenSince = Date()
    var seenWants = false
    var draftSig = ""
    var handledSigs: [String] = []
    static let glowKey = "glowHint"
    lazy var glowMark = PaisteApp.paperclipHalo()

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
        UserDefaults.standard.register(defaults: [Self.glowKey: true])
        Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            self?.remember()
        }
    }

    func startSpin() {
        stopGlow()
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
        let ok = Self.usable(scene)
        if ok {
            lastGood = scene
            lastGoodAt = Date()
            lastPID = frontPID
        }
        guard glowOn else {
            stopGlow()
            return
        }
        updateGlow(scene: scene, usable: ok, pid: frontPID)
    }

    // MARK: reply hint

    var glowOn: Bool { UserDefaults.standard.bool(forKey: Self.glowKey) }

    /// Reuses the scene the poll already read. No network, no capture.
    func updateGlow(scene: String, usable: Bool, pid: pid_t) {
        guard glowOn else {
            stopGlow()
            return
        }
        let doc = Self.sceneDoc(scene)
        let app = NSRunningApplication(processIdentifier: pid)
        let name = app?.localizedName ?? (doc["app"] as? String ?? "")
        let title = doc["title"] as? String ?? ""
        let sig = "\(pid)|" + name + "|" + title
        let wants = usable && Self.replyLikely(app: name, bundle: app?.bundleIdentifier ?? "", title: title, doc: doc)
        let now = Date()
        if sig != seenSig || wants != seenWants {
            seenSig = sig
            seenWants = wants
            seenSince = now
        }
        let stable = now.timeIntervalSince(seenSince) >= 1.5
        if !glowOn || !wants || handledSigs.contains(sig) || busy || spinTimer != nil {
            stopGlow()
            return
        }
        if glowSig != sig { stopGlow() }
        if stable && glowTimer == nil { startGlow(sig) }
    }

    /// Mail, chat, or a work web page, with message text that is not only the reply box.
    static func replyLikely(app: String, bundle: String, title: String, doc: [String: Any]) -> Bool {
        let name = app.lowercased()
        let id = bundle.lowercased()
        let low = title.lowercased()
        let mail = ["mail", "outlook", "spark"]
        let chat = ["slack", "teams", "messages", "workplace", "workchat", "zoom", "discord", "whatsapp", "telegram"]
        let pages = ["jira", "atlassian.net", "dev.azure.com", "visualstudio.com", "azure devops", "monday.com",
                     "gmail", "outlook", "slack", "microsoft teams", "teams"]
        let isMail = mail.contains { name == $0 || name.hasPrefix($0 + " ") || name.contains("microsoft " + $0) } || id == "com.apple.mail"
        let isChat = chat.contains { name.contains($0) } || id.contains("slack") || id.contains("teams") || id == "com.apple.mobilesms"
        let isBrowser = isBrowserBundle(id)
        guard isMail || isChat || (isBrowser && pages.contains { low.contains($0) }) else { return false }
        let mine = (doc["field_text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !(doc["selection"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
        let blocks = doc["blocks"] as? [[String: Any]] ?? []
        var other = 0
        for row in blocks {
            let text = (row["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty || text == mine || chrome(text) { continue }
            other += text.count
            if other >= 40 { return true }
        }
        return false
    }

    /// Browsers by bundle id. A name like "Edge Notes" or "Archive Utility" is not a browser.
    static func isBrowserBundle(_ bundle: String) -> Bool {
        let id = bundle.lowercased()
        let exact = ["com.apple.safari", "org.mozilla.firefox", "company.thebrowser.browser"]
        let prefixes = ["com.google.chrome", "com.microsoft.edgemac", "com.brave.browser",
                        "com.operasoftware.", "com.vivaldi.", "com.kagi.kimei"]
        return exact.contains(id) || prefixes.contains { id.hasPrefix($0) }
    }

    static func sceneDoc(_ scene: String) -> [String: Any] {
        guard let data = scene.data(using: .utf8),
              let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return doc
    }

    func startGlow(_ sig: String) {
        guard let button = item?.button else { return }
        glowSig = sig
        glowStart = Date()
        button.image = glowMark
        button.toolTip = "A reply may be needed — click to draft"
        glowTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 15.0, repeats: true) { [weak self] _ in
            guard let self, let button = self.item?.button else { return }
            if self.busy || self.spinTimer != nil {
                self.stopGlow()
                return
            }
            let t = Date().timeIntervalSince(self.glowStart)
            let wave = 0.5 - 0.5 * cos(2 * Double.pi * t / 1.6)
            button.alphaValue = CGFloat(0.45 + 0.55 * wave)
        }
    }

    func stopGlow() {
        guard glowTimer != nil || !glowSig.isEmpty else { return }
        glowTimer?.invalidate()
        glowTimer = nil
        glowSig = ""
        if let button = item?.button {
            button.alphaValue = 1
            if spinTimer == nil && button.image === glowMark {
                button.image = mark
                button.toolTip = "paisty"
            }
        }
    }

    func markHandled(_ sig: String) {
        guard !sig.isEmpty else { return }
        if !handledSigs.contains(sig) { handledSigs.append(sig) }
        if handledSigs.count > 40 { handledSigs.removeFirst(handledSigs.count - 40) }
        if glowSig == sig { stopGlow() }
    }

    @objc func toggleGlow(_ sender: NSMenuItem) {
        let next = !glowOn
        UserDefaults.standard.set(next, forKey: Self.glowKey)
        if !next { stopGlow() }
    }

    @objc func openSetup() {
        if board == nil { board = SetupBoard() }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        board?.show()
    }

    @objc func setupAutomatically() {
        openSetup()
        board?.runAuto()
    }

    @objc func scanAgain() {
        openSetup()
        board?.rescan()
    }

    @objc func click() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.option) == true {
            showMenu()
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
        draftSig = seenSig
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
            if !text.isEmpty {
                let path = self.deliver(text, pid: pid, field: field)
                DispatchQueue.main.async {
                    self.finish(path: path, note: captureNote)
                }
                return
            }
            DispatchQueue.main.async {
                self.stopSpin()
                if captureNote == "need-screen-recording" {
                    CGRequestScreenCaptureAccess()
                }
                let hint = blocked == "license" ? "Right-click and open setup to paste your key" : (captureNote == "need-screen-recording" ? "Allow paisty in Screen Recording" : "Nothing to copy")
                self.fail(hint)
                self.log.error("empty \(captureNote, privacy: .public)")
            }
        }
    }

    /// Puts the text on the clipboard, then tries to place it in the target app's focused field. Call off the main thread. Never presses Return.
    func deliver(_ text: String, pid: pid_t, field: AXUIElement?) -> String {
        DispatchQueue.main.sync {
            let board = NSPasteboard.general
            board.clearContents()
            board.setString(text, forType: .string)
        }
        let bundle = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? "?"
        var path = "copied"
        if pid != 0 {
            if let field, Self.alive(field, pid: pid), let won = Self.insert(text, into: field, pid: pid) {
                path = won
            } else {
                Self.activate(pid)
                if let fresh = Self.focusedInput(pid: pid, deep: true), let won = Self.insert(text, into: fresh, pid: pid) {
                    path = won
                } else if CGPreflightScreenCaptureAccess(), let composer = Self.composerPoint(pid: pid),
                          Self.clickPaste(pid: pid, at: composer) {
                    path = "ocr-cmdv"
                }
            }
        }
        log.error("deliver \(path, privacy: .public) app \(bundle, privacy: .public)")
        return path
    }

    func finish(path: String, note: String = "") {
        stopSpin()
        markHandled(draftSig)
        let pasted = path != "copied"
        item?.button?.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: pasted ? "Pasted" : "Copied")
        item?.button?.toolTip = pasted ? "Pasted" : "Copied"
        item?.button?.appearsDisabled = false
        log.error("\(pasted ? "pasted" : "copied", privacy: .public) \(path, privacy: .public) \(note, privacy: .public)")
        busy = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            self.item?.button?.image = self.mark
            self.item?.button?.toolTip = "paisty"
        }
    }

    func fail(_ hint: String) {
        stopSpin()
        item?.button?.image = NSImage(systemSymbolName: "questionmark", accessibilityDescription: hint)
        item?.button?.toolTip = hint
        item?.button?.appearsDisabled = false
        busy = false
    }

    // MARK: right-click menu

    struct Draft {
        let id: String
        let at: String
        let preview: String
        let app: String
        let text: String
    }

    final class DraftRef: NSObject {
        let draft: Draft
        let pid: pid_t
        let field: AXUIElement?
        init(_ draft: Draft, pid: pid_t, field: AXUIElement?) {
            self.draft = draft
            self.pid = pid
            self.field = field
        }
    }

    func showMenu() {
        let pid = frontPID
        let field = (pastePID == pid && Date().timeIntervalSince(pasteAt) < 8) ? pasteTarget : nil
        DispatchQueue.global(qos: .userInitiated).async {
            let drafts = Self.loadDrafts()
            DispatchQueue.main.async {
                guard let item = self.item, let button = item.button else { return }
                item.menu = self.buildMenu(drafts, pid: pid, field: field)
                button.performClick(nil)
                item.menu = nil
            }
        }
    }

    func buildMenu(_ drafts: [Draft], pid: pid_t, field: AXUIElement?) -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.addItem(header("Recent drafts"))
        if drafts.isEmpty {
            menu.addItem(header("No drafts yet — left-click the paperclip to draft"))
        }
        for draft in drafts.prefix(8) {
            let ref = DraftRef(draft, pid: pid, field: field)
            let row = NSMenuItem(title: "", action: #selector(pasteDraft(_:)), keyEquivalent: "")
            row.target = self
            row.representedObject = ref
            row.attributedTitle = Self.rowTitle(draft)
            row.isEnabled = !busy
            let sub = NSMenu()
            sub.autoenablesItems = false
            let paste = NSMenuItem(title: "Paste this draft", action: #selector(pasteDraft(_:)), keyEquivalent: "")
            paste.target = self
            paste.representedObject = ref
            sub.addItem(paste)
            let redo = NSMenuItem(title: "Redo (write a new one)", action: #selector(redoDraft(_:)), keyEquivalent: "")
            redo.target = self
            redo.representedObject = ref
            sub.addItem(redo)
            row.submenu = sub
            menu.addItem(row)
        }
        menu.addItem(.separator())
        menu.addItem(header("Setup"))
        let auto = NSMenuItem(title: "Set up automatically", action: #selector(setupAutomatically), keyEquivalent: "")
        auto.target = self
        menu.addItem(auto)
        let setup = NSMenuItem(title: "Open setup…", action: #selector(openSetup), keyEquivalent: "")
        setup.target = self
        menu.addItem(setup)
        let scan = NSMenuItem(title: "Scan this Mac again", action: #selector(scanAgain), keyEquivalent: "")
        scan.target = self
        menu.addItem(scan)
        let glow = NSMenuItem(title: "Glow when a reply may be needed", action: #selector(toggleGlow(_:)), keyEquivalent: "")
        glow.target = self
        glow.state = glowOn ? .on : .off
        menu.addItem(glow)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit paisty", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        quit.target = NSApp
        menu.addItem(quit)
        return menu
    }

    func header(_ title: String) -> NSMenuItem {
        let row = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        row.isEnabled = false
        return row
    }

    static func rowTitle(_ draft: Draft) -> NSAttributedString {
        var preview = draft.preview.isEmpty ? draft.text : draft.preview
        preview = preview.replacingOccurrences(of: "\n", with: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        if preview.count > 60 { preview = String(preview.prefix(59)) + "…" }
        let title = NSMutableAttributedString(string: preview.isEmpty ? "(empty draft)" : preview, attributes: [.font: NSFont.menuFont(ofSize: 0)])
        let suffix = [draft.app, ago(draft.at)].filter { !$0.isEmpty }.joined(separator: " · ")
        if !suffix.isEmpty {
            title.append(NSAttributedString(string: "  " + suffix, attributes: [
                .font: NSFont.menuFont(ofSize: NSFont.smallSystemFontSize),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
        }
        return title
    }

    static func ago(_ at: String) -> String {
        var date: Date?
        if let seconds = Double(at) {
            date = Date(timeIntervalSince1970: seconds > 1e12 ? seconds / 1000 : seconds)
        } else {
            let iso = ISO8601DateFormatter()
            date = iso.date(from: at)
            if date == nil {
                iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                date = iso.date(from: at)
            }
        }
        guard let date else { return String(at.prefix(16)) }
        let gap = Date().timeIntervalSince(date)
        if gap < 60 { return "now" }
        if gap < 3600 { return "\(Int(gap / 60))m" }
        if gap < 86400 { return "\(Int(gap / 3600))h" }
        let shown = DateFormatter()
        shown.dateFormat = "MMM d"
        return shown.string(from: date)
    }

    static func loadDrafts() -> [Draft] {
        let run = runPaiste(["drafts"], timeout: 3)
        guard run.status == 0, let data = run.out.data(using: .utf8),
              let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        func text(_ row: [String: Any], _ key: String) -> String {
            switch row[key] {
            case let value as String: return value
            case let value as NSNumber: return value.stringValue
            default: return ""
            }
        }
        return rows.compactMap { row in
            let id = text(row, "id")
            if id.isEmpty { return nil }
            return Draft(id: id, at: text(row, "at"), preview: text(row, "preview"), app: text(row, "app"), text: text(row, "text"))
        }
    }

    @objc func pasteDraft(_ sender: NSMenuItem) {
        guard let ref = sender.representedObject as? DraftRef, !busy else { return }
        busy = true
        draftSig = seenSig
        DispatchQueue.global(qos: .userInitiated).async {
            var text = ref.draft.text
            if text.isEmpty {
                let run = Self.runPaiste(["show", "--id", ref.draft.id], timeout: 5)
                text = run.status == 0 ? run.out : ""
            }
            if text.isEmpty {
                DispatchQueue.main.async { self.fail("That draft is empty") }
                return
            }
            let path = self.deliver(text, pid: ref.pid, field: ref.field)
            DispatchQueue.main.async { self.finish(path: path, note: "menu-paste") }
        }
    }

    @objc func redoDraft(_ sender: NSMenuItem) {
        guard let ref = sender.representedObject as? DraftRef, !busy else { return }
        busy = true
        draftSig = seenSig
        startSpin()
        DispatchQueue.global(qos: .userInitiated).async {
            let run = Self.runPaiste(["redo", "--id", ref.draft.id], timeout: 90)
            guard run.status == 0, !run.out.isEmpty else {
                DispatchQueue.main.async {
                    let hint = run.out == "no scene" ? "That older draft can't be redone" : (run.status == 2 ? "Redo found nothing to draft" : "Redo failed")
                    self.fail(hint)
                    self.log.error("redo failed \(run.status, privacy: .public)")
                }
                return
            }
            let path = self.deliver(run.out, pid: ref.pid, field: ref.field)
            DispatchQueue.main.async { self.finish(path: path, note: "menu-redo") }
        }
    }

    static func paisteProcess(_ args: [String]) -> Process {
        let task = Process()
        let script = paisteCommand()
        if script == "hailports" {
            task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            task.arguments = ["hailports", "paiste"] + args
        } else if script.hasSuffix("/hailports") {
            task.executableURL = URL(fileURLWithPath: script)
            task.arguments = ["paiste"] + args
        } else {
            task.executableURL = URL(fileURLWithPath: script)
            task.arguments = args
        }
        return task
    }

    /// Runs the paiste CLI, kills it after `timeout` seconds. Returns trimmed stdout and exit status (-1 on launch failure or timeout).
    static func runPaiste(_ args: [String], timeout: TimeInterval) -> (out: String, status: Int32) {
        let task = paisteProcess(args)
        let out = Pipe()
        task.standardOutput = out
        task.standardError = FileHandle.nullDevice
        let done = DispatchSemaphore(value: 0)
        task.terminationHandler = { _ in done.signal() }
        do { try task.run() } catch { return ("", -1) }
        var data = Data()
        let read = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .utility).async {
            data = out.fileHandleForReading.readDataToEndOfFile()
            read.signal()
        }
        if done.wait(timeout: .now() + timeout) == .timedOut {
            task.terminate()
            _ = read.wait(timeout: .now() + 1)
            return ("", -1)
        }
        _ = read.wait(timeout: .now() + 2)
        let text = (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return (text, task.terminationStatus)
    }

    static func skipsFront(_ app: NSRunningApplication) -> Bool {
        let name = (app.localizedName ?? "").lowercased()
        let id = (app.bundleIdentifier ?? "").lowercased()
        if id == "com.hailports.paiste" { return true }
        let keys = ["clippy", "maccy", "flycut", "copyclip", "pastebot"]
        if keys.contains(where: { name.contains($0) || id.contains($0) }) { return true }
        return name == "paste"
    }

    static var manualAX = Set<pid_t>()
    static let manualAXLock = NSLock()

    static func focusedInput(pid: pid_t, deep: Bool = false) -> AXUIElement? {
        let app = AXUIElementCreateApplication(pid)
        manualAXLock.lock()
        let fresh = manualAX.insert(pid).inserted
        manualAXLock.unlock()
        if fresh {
            // Electron (Slack, Teams) only exposes its text fields once this is set.
            AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        }
        var candidates: [AXUIElement] = []
        if let el = element(app, kAXFocusedUIElementAttribute) { candidates.append(el) }
        if let window = element(app, kAXFocusedWindowAttribute), let el = element(window, kAXFocusedUIElementAttribute) {
            candidates.append(el)
        }
        for el in candidates {
            if let target = editableTarget(el) { return target }
        }
        // New Outlook and other web-backed composers expose a real AXTextArea for the
        // message body that is not the app-level focused element, so the probes above
        // miss it. Only on the paste path (deep), and only after they return nothing,
        // search the window for the body field. This cannot change an app that already
        // resolved a target above.
        if deep,
           let window = element(app, kAXFocusedWindowAttribute) ?? element(app, kAXMainWindowAttribute),
           let found = bestEditable(in: window) {
            return found
        }
        return nil
    }

    /// Depth- and breadth-bounded search for the largest editable text box in a window.
    /// Prefers an AXTextArea (a message body) over a one-line AXTextField (a search box).
    static func bestEditable(in root: AXUIElement) -> AXUIElement? {
        var queue: [(AXUIElement, Int)] = [(root, 0)]
        var scanned = 0
        var best: AXUIElement?
        var bestScore: Double = -1
        while !queue.isEmpty, scanned < 1600 {
            let (node, depth) = queue.removeFirst()
            scanned += 1
            if depth > 0, !excluded(node), isEditable(node) {
                let role = str(node, kAXRoleAttribute) ?? ""
                let dims = box(node)
                let area = (dims["w"] ?? 0) * (dims["h"] ?? 0)
                var score = area
                if role == "AXTextArea" { score += 5_000_000 }
                if role == "AXTextArea" || area >= 6000 {
                    if score > bestScore { bestScore = score; best = node }
                }
            }
            if depth >= 12 { continue }
            var kids: CFTypeRef?
            if AXUIElementCopyAttributeValue(node, kAXChildrenAttribute as CFString, &kids) == .success,
               let list = kids as? [AXUIElement] {
                for child in list.prefix(60) { queue.append((child, depth + 1)) }
            }
        }
        return best
    }

    static func element(_ el: AXUIElement, _ attr: String) -> AXUIElement? {
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &raw) == .success, let raw,
              CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
        return (raw as! AXUIElement)
    }

    static func settable(_ el: AXUIElement, _ attr: String) -> Bool {
        var flag = DarwinBoolean(false)
        return AXUIElementIsAttributeSettable(el, attr as CFString, &flag) == .success && flag.boolValue
    }

    static func excluded(_ el: AXUIElement) -> Bool {
        let role = (str(el, kAXRoleAttribute) ?? "").lowercased()
        let sub = (str(el, kAXSubroleAttribute) ?? "").lowercased()
        return role.contains("secure") || sub.contains("search") || sub.contains("secure")
    }

    /// The element to paste into: the focused element itself when it takes text, else its editable ancestor (web contenteditable).
    static func editableTarget(_ el: AXUIElement) -> AXUIElement? {
        if excluded(el) { return nil }
        if isEditable(el) { return el }
        for attr in ["AXEditableAncestor", "AXHighestEditableAncestor"] {
            if let up = element(el, attr), !excluded(up), settable(up, kAXSelectedTextAttribute) {
                return up
            }
        }
        if element(el, "AXEditableAncestor") != nil || element(el, "AXHighestEditableAncestor") != nil {
            return el
        }
        return nil
    }

    static func isEditable(_ el: AXUIElement) -> Bool {
        if excluded(el) { return false }
        let role = (str(el, kAXRoleAttribute) ?? "")
        if role == "AXTextArea" || role == "AXTextField" {
            return settable(el, kAXSelectedTextAttribute) || settable(el, kAXValueAttribute)
        }
        if element(el, "AXEditableAncestor") != nil || element(el, "AXHighestEditableAncestor") != nil {
            return true
        }
        if role == "AXWebArea" || role == "AXGroup" {
            return settable(el, kAXSelectedTextAttribute)
        }
        return false
    }

    /// The focused element still exists and belongs to the target app.
    static func alive(_ el: AXUIElement, pid: pid_t) -> Bool {
        var owner: pid_t = 0
        guard AXUIElementGetPid(el, &owner) == .success, owner == pid else { return false }
        return str(el, kAXRoleAttribute) != nil
    }

    static func activate(_ pid: pid_t) {
        guard let app = NSRunningApplication(processIdentifier: pid) else { return }
        if !app.isActive {
            app.activate(options: [.activateIgnoringOtherApps])
            Thread.sleep(forTimeInterval: 0.18)
        }
    }

    /// A snapshot used to tell whether an AX write changed anything.
    static func signature(_ el: AXUIElement) -> String {
        var parts: [String] = []
        parts.append(str(el, kAXValueAttribute) ?? "-")
        for attr in [kAXNumberOfCharactersAttribute, kAXSelectedTextRangeAttribute] {
            var raw: CFTypeRef?
            if AXUIElementCopyAttributeValue(el, attr as CFString, &raw) == .success, let raw {
                parts.append(String(describing: raw))
            } else {
                parts.append("-")
            }
        }
        return parts.joined(separator: "|")
    }

    /// Returns "ax-insert" or "ax-cmdv" on success, nil when the field would not take the text. Never presses Return.
    static func insert(_ text: String, into el: AXUIElement, pid: pid_t) -> String? {
        let before = signature(el)
        AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        if AXUIElementSetAttributeValue(el, kAXSelectedTextAttribute as CFString, text as CFString) == .success {
            Thread.sleep(forTimeInterval: 0.12)
            if signature(el) != before { return "ax-insert" }
        } else {
            let role = str(el, kAXRoleAttribute) ?? ""
            if role == "AXTextArea" || role == "AXTextField" {
                let existing = (str(el, kAXValueAttribute) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if existing.count <= 1,
                   AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, text as CFString) == .success {
                    return "ax-insert"
                }
            }
        }
        guard isEditable(el) || editableTarget(el) != nil else { return nil }
        activate(pid)
        AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        return commandV() ? "ax-cmdv" : nil
    }

    /// Cmd-V into the frontmost app. Only Cmd-V; no Return.
    static func commandV() -> Bool {
        guard let source = CGEventSource(stateID: .combinedSessionState) else { return false }
        let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: true)
        keyDown?.flags = .maskCommand
        let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: false)
        keyUp?.flags = .maskCommand
        keyDown?.post(tap: .cgSessionEventTap)
        keyUp?.post(tap: .cgSessionEventTap)
        return keyDown != nil
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
        let outlook = (NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? "").lowercased().hasPrefix("com.microsoft.outlook")
        var best: CGRect?
        for item in request.results ?? [] {
            guard let text = item.topCandidates(1).first?.string.lowercased() else { continue }
            let box = item.boundingBox
            if isComposerLabel(text) {
                guard box.midY < 0.4 else { continue }
            } else if outlook && isOutlookBodyLabel(text) {
                guard box.midY < 0.6 else { continue }
            } else {
                continue
            }
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

    /// New Outlook compose body placeholders. The subject line is not a body.
    static func isOutlookBodyLabel(_ text: String) -> Bool {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if line.contains("subject") { return false }
        let buttons: Set<String> = ["reply", "reply all", "forward", "compose", "new mail", "new message"]
        if buttons.contains(line) { return false }
        let hints = ["type /", "write your message", "compose", "reply"]
        return line.count < 80 && hints.contains(where: { line.contains($0) })
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
        return commandV()
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

    /// The paperclip with a small dot: a reply may be needed. Template, so it follows the menu bar.
    static func paperclipHalo() -> NSImage {
        let side: CGFloat = 18
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            NSColor.black.setStroke()
            clipPath().stroke()
            NSColor.black.setFill()
            NSBezierPath(ovalIn: NSRect(x: 13.2, y: 13.2, width: 4.4, height: 4.4)).fill()
            return true
        }
        image.isTemplate = true
        return image
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
        let task = paisteProcess(["draft", "--scene", url.path])
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


