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
    var spinTimer: Timer?          // the thinking pulse timer (kept name: the glow checks it)
    var pulse = PulseMachine()
    var pulseStart = Date()
    var windowDrafts: [String: LastDraft] = [:]
    var redraftPins = RedraftPins()   // the open Redraft popup's pins
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
    var trustTimer: Timer?
    var askedPasteAccess = false

    /// paisty://activate?k=… from the key email or the thanks page turns the app on.
    func applicationWillFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(openURL(_:reply:)),
                                                     forEventClass: AEEventClass(kInternetEventClass), andEventID: AEEventID(kAEGetURL))
    }

    @objc func openURL(_ event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let raw = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
              let url = URL(string: raw), let key = PaistyLicense.activationKey(from: url) else { return }
        if PaistyLicense.save(key) { activated() }
    }

    func activated() {
        board?.licenseOnly = false
        item?.button?.toolTip = "paisty is on"
        PaistyUpdater.shared.check(userAsked: false)
    }

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
        let fresh = !FileManager.default.fileExists(atPath: NSHomeDirectory() + "/.config/paiste")
        if fresh || !FileManager.default.fileExists(atPath: route) || !AXIsProcessTrusted() {
            openSetup()
        }
        UserDefaults.standard.register(defaults: [Self.glowKey: true])
        PaistyUpdater.shared.isBusy = { [weak self] in self?.busy ?? false }
        PaistyUpdater.shared.start()
        // No key yet: pick up the one the email form just issued to this network. Nothing to paste.
        if SetupBoard.licenseState() != .valid {
            PaistyLicense.claimUntilActive { [weak self] in self?.activated() }
        }
        Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            self?.remember()
        }
    }

    /// Spotlight or Applications relaunch while paisty is already running.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openSetup()
        return true
    }

    /// Thinking: the paperclip breathes and soft rings move outward. Never a spinner, which reads
    /// as stuck or broken. Reduce Motion gets a slow opacity breath only.
    func startSpin() {
        stopGlow()
        spinTimer?.invalidate()
        pulse.send(.start)
        pulseStart = Date()
        guard let button = item?.button else { return }
        button.appearsDisabled = false
        button.alphaValue = 1
        button.toolTip = "paisty is thinking…"
        button.setAccessibilityLabel("paisty is thinking")
        button.image = Self.thinkingImage(PulseSpec.frame(at: 0, reduceMotion: Self.reduceMotion))
        spinTimer = Timer(timeInterval: 1.0 / PulseSpec.fps, repeats: true) { [weak self] _ in
            guard let self, let button = self.item?.button else { return }
            let t = Date().timeIntervalSince(self.pulseStart)
            button.image = Self.thinkingImage(PulseSpec.frame(at: t, reduceMotion: Self.reduceMotion))
        }
        // Common modes: the pulse keeps moving while a menu is open.
        RunLoop.main.add(spinTimer!, forMode: .common)
    }

    func stopSpin() {
        spinTimer?.invalidate()
        spinTimer = nil
        item?.button?.alphaValue = 1
        item?.button?.setAccessibilityLabel("paisty")
    }

    static var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

    /// One frame of the pulse as a template image, so it follows the light or dark menu bar.
    static func thinkingImage(_ frame: PulseFrame) -> NSImage {
        let side = PulseSpec.canvas
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            let centre = NSPoint(x: side / 2, y: side / 2)
            for ring in frame.rings where ring.alpha > 0.01 {
                let r = ring.radius
                let circle = NSBezierPath(ovalIn: NSRect(x: centre.x - r, y: centre.y - r, width: 2 * r, height: 2 * r))
                circle.lineWidth = ring.width
                NSColor.black.withAlphaComponent(ring.alpha).setStroke()
                circle.stroke()
            }
            let clip = clipPath()
            var t = AffineTransform()
            t.translate(x: centre.x, y: centre.y)
            t.scale(frame.scale)
            t.translate(x: -clip.bounds.midX, y: -clip.bounds.midY)   // centre the paperclip
            let weight = clip.lineWidth
            clip.transform(using: t)
            clip.lineWidth = weight * frame.scale
            NSColor.black.withAlphaComponent(frame.opacity).setStroke()
            clip.stroke()
            return true
        }
        image.isTemplate = true
        return image
    }

    func watchTrust() {
        trustTimer?.invalidate()
        trustTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] timer in
            guard let self, let board = self.board, board.window.isVisible else {
                timer.invalidate()
                self?.trustTimer = nil
                return
            }
            board.refreshTrust()
            if AXIsProcessTrusted(), self.item?.button?.toolTip == "Copied. Turn on Accessibility to paste automatically." {
                self.item?.button?.toolTip = "paisty"
                self.item?.button?.image = self.mark
            }
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
        let pages = ["jira", "atlassian.net", "dev.az" + "ure.com", "visualstudio.com", "azure devops", "monda" + "y.com",
                     "gmail", "outlook", "slack", "microsoft teams", "teams"]
        let exxxxx = mail.contains { name == $0 || name.hasPrefix($0 + " ") || name.contains("microsoft " + $0) } || id == "com.apple.mail"
        let isChat = chat.contains { name.contains($0) } || id.contains("slack") || id.contains("teams") || id == "com.apple.mobilesms"
        let isBrowser = isBrowserBundle(id)
        guard exxxxx || isChat || (isBrowser && pages.contains { low.contains($0) }) else { return false }
        if !(doc["selection"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
        // Only when the newest message on screen is someone else's and asks something.
        // Tab titles, my own item, and an already-answered thread do not glow.
        return ReplyHint.waiting(
            fieldText: doc["field_text"] as? String ?? "",
            blocks: doc["blocks"] as? [[String: Any]] ?? [],
            title: title,
            me: meNames,
            newestFirst: ReplyHint.newestFirst(title: title, bundle: bundle)
        )
    }

    static let meNames: [String] = {
        let route = ProcessInfo.processInfo.environment["PAISTE_ROUTE"] ?? (NSHomeDirectory() + "/.config/paiste/route.yaml")
        let text = (try? String(contentsOfFile: route, encoding: .utf8)) ?? ""
        return ReplyHint.meNames(routeText: text, fullName: NSFullUserName())
    }()

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
        pulse.send(.hintOn)
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
        pulse.send(.hintOff)
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

    @objc func toggleLookups(_ sender: NSMenuItem) {
        Self.writeLookups(!Self.lookupsOn())
    }

    static func routePath() -> String {
        let route = ProcessInfo.processInfo.environment["PAISTE_ROUTE"] ?? ""
        if !route.isEmpty { return route }
        return NSHomeDirectory() + "/.config/paiste/route.yaml"
    }

    static func lookupFilePath() -> String {
        return (NSHomeDirectory() as NSString).appendingPathComponent(".config/paiste/lookups")
    }

    static func lookupsOn() -> Bool {
        if let text = try? String(contentsOfFile: routePath(), encoding: .utf8) {
            for line in text.components(separatedBy: "\n") {
                if line.hasPrefix("lookups:") {
                    let value = line.dropFirst("lookups:".count).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                    return value == "on" || value == "true" || value == "yes" || value == "1"
                }
            }
        }
        let side = (try? String(contentsOfFile: lookupFilePath(), encoding: .utf8)) ?? ""
        let value = side.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return value == "on" || value == "true" || value == "yes" || value == "1"
    }

    static func writeLookups(_ on: Bool) {
        let word = on ? "on" : "off"
        let route = routePath()
        if FileManager.default.fileExists(atPath: route),
           var text = try? String(contentsOfFile: route, encoding: .utf8) {
            var lines = text.components(separatedBy: "\n")
            var hit = false
            for index in lines.indices where lines[index].hasPrefix("lookups:") {
                lines[index] = "lookups: " + word
                hit = true
                break
            }
            if hit {
                text = lines.joined(separator: "\n")
                if !text.hasSuffix("\n") { text += "\n" }
            } else if text.isEmpty {
                text = "lookups: " + word + "\n"
            } else if text.hasSuffix("\n") {
                text += "lookups: " + word + "\n"
            } else {
                text += "\nlookups: " + word + "\n"
            }
            try? text.write(toFile: route, atomically: true, encoding: .utf8)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: route)
        }
        let side = lookupFilePath()
        let dir = (side as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try? (word + "\n").write(toFile: side, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: side)
    }

    @objc func openSetup() {
        if board == nil { board = SetupBoard() }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        // First-run and menu "Open setup" keep the full wizard when no key exists yet.
        // A saved key that is expired or malformed opens the recovery screen.
        if SetupBoard.hasExpiredOrInvalidLicense() {
            board?.showLicense()
        } else {
            board?.licenseOnly = false
            board?.show()
        }
        watchTrust()
    }

    func openLicense() {
        if board == nil { board = SetupBoard() }
        NSApp.setActivationPolicy(.regular)
        board?.showLicense()
        watchTrust()
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
        let gesture = event?.type == .rightMouseUp || event?.modifierFlags.contains(.option) == true
        let window = Self.windowKey(pid: frontPID)
        switch ClickRouter.decide(menuGesture: gesture, busy: busy, last: windowDrafts[window], window: window, now: Date()) {
        case .menu:
            showMenu()
            return
        case .ignore:
            return
        case .offerRedraft:
            if let last = windowDrafts[window] { showRedraftPopup(last) }
            return
        case .draft:
            startDraft(prior: nil, window: window)
        }
    }

    /// "pid|bundle|window title": paisty keeps one last draft per window.
    static func windowKey(pid: pid_t) -> String {
        guard pid != 0 else { return "" }
        let app = AXUIElementCreateApplication(pid)
        var title = ""
        if let win = PastePath.element(app, kAXFocusedWindowAttribute) {
            title = PastePath.str(win, kAXTitleAttribute) ?? ""
        }
        let bundle = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? "?"
        return "\(pid)|\(bundle)|\(title)"
    }

    /// Second click on a window paisty already drafted for.
    func showRedraftPopup(_ last: LastDraft) {
        let menu = NSMenu()
        menu.autoenablesItems = false
        let head = NSMenuItem(title: last.placed ? "paisty drafted a reply here" : "paisty copied a reply for this window", action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        // Which sentences stay: the user's edits start pinned, a tap pins or unpins one.
        let field = Self.readField(pid: frontPID, remembered: pastePID == frontPID ? pasteTarget : nil)
        redraftPins = RedraftPins.from(json: field.flatMap { Self.lockRows(field: $0, prior: last.text) },
                                       fieldReadable: field != nil || !last.placed)
        if let note = redraftPins.note {
            let line = NSMenuItem(title: note, action: nil, keyEquivalent: "")
            line.isEnabled = false
            menu.addItem(line)
        }
        if !redraftPins.rows.isEmpty {
            let hint = NSMenuItem(title: "Pinned sentences stay as you wrote them", action: nil, keyEquivalent: "")
            hint.isEnabled = false
            menu.addItem(hint)
            for (index, sentence) in redraftPins.rows.enumerated() {
                menu.addItem(pinRow(sentence, index: index))
            }
            menu.addItem(.separator())
        }
        let redo = NSMenuItem(title: "Redraft", action: #selector(redraftChosen(_:)), keyEquivalent: "r")
        redo.target = self
        redo.representedObject = last.window
        redo.toolTip = "Reads the screen again, keeps pinned sentences word for word, and replaces the earlier draft"
        menu.addItem(redo)
        let fresh = NSMenuItem(title: "Fresh redraft", action: #selector(freshRedraftChosen(_:)), keyEquivalent: "f")
        fresh.target = self
        fresh.representedObject = last.window
        fresh.toolTip = "Ignores pins and edits and writes a new reply from scratch"
        menu.addItem(fresh)
        let copy = NSMenuItem(title: "Copy again", action: #selector(copyAgainChosen(_:)), keyEquivalent: "c")
        copy.target = self
        copy.representedObject = last.window
        menu.addItem(copy)
        menu.addItem(.separator())
        let cancel = NSMenuItem(title: "Cancel", action: nil, keyEquivalent: "\u{1b}")
        cancel.isEnabled = true
        menu.addItem(cancel)
        guard let item, let button = item.button else { return }
        item.menu = menu
        button.performClick(nil)
        item.menu = nil
    }

    @objc func redraftChosen(_ sender: NSMenuItem) {
        guard let window = sender.representedObject as? String, let last = windowDrafts[window], !busy else { return }
        startDraft(prior: last, window: window, locks: redraftPins.request(fresh: false))
    }

    @objc func freshRedraftChosen(_ sender: NSMenuItem) {
        guard let window = sender.representedObject as? String, let last = windowDrafts[window], !busy else { return }
        startDraft(prior: last, window: window, locks: redraftPins.request(fresh: true))
    }

    /// A sentence row: a borderless toggle with a pin glyph, template ink like the rest of the menu.
    /// Clicking a control inside a menu item view does not close the menu, so several pins take several taps.
    func pinRow(_ sentence: RedraftSentence, index: Int) -> NSMenuItem {
        let row = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        let button = NSButton(title: RedraftPins.label(sentence.text), target: self, action: #selector(pinToggled(_:)))
        button.setButtonType(.toggle)
        button.isBordered = false
        button.image = NSImage(systemSymbolName: "pin", accessibilityDescription: "Not pinned")
        button.alternateImage = NSImage(systemSymbolName: "pin.fill", accessibilityDescription: "Pinned")
        button.imagePosition = .imageLeading
        button.alignment = .left
        button.font = NSFont.menuFont(ofSize: 0)
        button.contentTintColor = .labelColor
        button.state = sentence.pinned ? .on : .off
        button.tag = index
        button.toolTip = sentence.text
        button.frame = NSRect(x: 14, y: 1, width: 330, height: 20)
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 350, height: 22))
        view.addSubview(button)
        row.view = view
        return row
    }

    @objc func pinToggled(_ sender: NSButton) {
        redraftPins.toggle(sender.tag)
    }

    /// The reply box text, without bringing anything forward. nil when it cannot be read.
    static func readField(pid: pid_t, remembered: AXUIElement?) -> String? {
        guard AXIsProcessTrusted(), pid != 0 else { return nil }
        var target = remembered
        if target == nil || !alive(target!, pid: pid) {
            target = focusedInput(pid: pid, deep: true)
        }
        return target.flatMap { PastePath.str($0, kAXValueAttribute) }
    }

    /// The earlier draft's sentences now in the box, edits pre-pinned, from the bundled helper.
    static func lockRows(field: String, prior: String) -> [String: Any]? {
        let dir = FileManager.default.temporaryDirectory
        let f = dir.appendingPathComponent("paiste-field-locks.txt")
        let p = dir.appendingPathComponent("paiste-prior-locks.txt")
        try? field.data(using: .utf8)?.write(to: f)
        try? prior.data(using: .utf8)?.write(to: p)
        defer {
            try? FileManager.default.removeItem(at: f)
            try? FileManager.default.removeItem(at: p)
        }
        let run = runPaiste(["locks", "--field", f.path, "--prior", p.path], timeout: 3)
        guard run.status == 0, let data = run.out.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    @objc func copyAgainChosen(_ sender: NSMenuItem) {
        guard let window = sender.representedObject as? String, let last = windowDrafts[window] else { return }
        let board = NSPasteboard.general
        board.clearContents()
        board.setString(last.text, forType: .string)
        item?.button?.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: "Copied")
        item?.button?.toolTip = "Copied again — press Cmd-V where you want it"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            if !self.busy {
                self.item?.button?.image = self.mark
                self.item?.button?.toolTip = "paisty"
            }
        }
    }

    /// Draft for the front window. With `prior`, a redraft: paisty's earlier text on screen is its own,
    /// the user's edits to it are kept, and the new reply replaces it instead of landing under it.
    func startDraft(prior: LastDraft?, window: String, locks: [String: Any]? = nil) {
        if !AXIsProcessTrusted() && !askedPasteAccess {
            askedPasteAccess = true
            openSetup()
        }
        busy = true
        draftSig = seenSig
        startSpin()
        let priorText = prior?.text ?? ""
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
                let drafted = Self.draft(scene, prior: priorText, locks: locks)
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
                        let drafted = Self.draft(raw, prior: priorText, locks: locks)
                        text = drafted.reply
                        blocked = drafted.blocked
                        if text.isEmpty { captureNote = "screen-unusable" }
                    }
                }
            }
            if !text.isEmpty {
                let path = prior != nil
                    ? self.deliverRedraft(text, pid: pid, field: field, prior: prior!)
                    : self.deliver(text, pid: pid, field: field)
                DispatchQueue.main.async {
                    ClickRouter.remember(LastDraft(text: text, at: Date(), window: window, path: path),
                                         in: &self.windowDrafts, now: Date())
                    self.finish(path: path, note: (prior != nil ? "redraft " : "") + captureNote)
                }
                return
            }
            DispatchQueue.main.async {
                self.stopSpin()
                if captureNote == "need-screen-recording" {
                    CGRequestScreenCaptureAccess()
                }
                if blocked == "license" { self.openLicense() }
                let hint = blocked == "license" ? "Your trial ended" : (captureNote == "need-screen-recording" ? "Allow paisty in Screen Recording" : "Nothing to copy")
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
        if !AXIsProcessTrusted() {
            PastePath.note("  untrusted clipboard only")
            return "copied-untrusted"
        }
        let bundle = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? "?"
        var path = "copied"
        PastePath.note("deliver app=\(bundle) chars=\(text.count) remembered=\(PastePath.describe(field)) front=\(PastePath.frontPID() == pid)")
        if pid != 0 {
            var remembered: AXUIElement?
            if let field, Self.alive(field, pid: pid) {
                remembered = field
                if let won = Self.insert(text, into: field, pid: pid) { path = won }
            }
            if path == "copied" {
                Self.activate(pid)
                let fresh = Self.focusedInput(pid: pid, deep: true)
                PastePath.note("  deep target \(PastePath.describe(fresh))")
                if let fresh, remembered == nil || !CFEqual(fresh, remembered!),
                   let won = Self.insert(text, into: fresh, pid: pid) {
                    path = won
                }
            }
            if path == "copied", CGPreflightScreenCaptureAccess(), PastePath.ensureFront(pid),
               let composer = Self.composerPoint(pid: pid), Self.clickPaste(pid: pid, at: composer) {
                path = "ocr-cmdv"
            }
        }
        PastePath.note("  final path=\(path)")
        log.error("deliver \(path, privacy: .public) app \(bundle, privacy: .public)")
        return path
    }

    /// Place a redraft over paisty's earlier draft. Call off the main thread. Never appends a second
    /// reply under the first: when the old draft can't be selected safely it stays on the clipboard.
    func deliverRedraft(_ text: String, pid: pid_t, field: AXUIElement?, prior: LastDraft) -> String {
        DispatchQueue.main.sync {
            let board = NSPasteboard.general
            board.clearContents()
            board.setString(text, forType: .string)
        }
        guard AXIsProcessTrusted(), pid != 0 else { return "copied-untrusted" }
        var target = field
        if target == nil || !Self.alive(target!, pid: pid) {
            Self.activate(pid)
            target = Self.focusedInput(pid: pid, deep: true)
        }
        let value = target.flatMap { PastePath.str($0, kAXValueAttribute) }
        var plan: [String: Any] = ["mode": "insert"]
        if let value {
            plan = Self.locatePlan(field: value, prior: prior.text)
        }
        let mode = ReplaceMode.from(plan: plan, fieldReadable: value != nil, previouslyPlaced: prior.placed)
        PastePath.note("redraft plan=\(plan["mode"] ?? "?")/\(plan["kind"] ?? "?") mode=\(mode)")
        switch mode {
        case .insert:
            return deliver(text, pid: pid, field: target)
        case .clipboardOnly:
            return "copied-redraft"
        case .replace(let start, let length):
            guard let el = target else { return "copied-redraft" }
            return Self.replaceRange(el, start16: start, length16: length, with: text, pid: pid) ?? "copied-redraft"
        }
    }

    /// Select the old draft by its range, then put the new text over the selection. Verified by reading back.
    static func replaceRange(_ el: AXUIElement, start16: Int, length16: Int, with text: String, pid: pid_t) -> String? {
        var range = CFRange(location: start16, length: length16)
        guard let axRange = AXValueCreate(.cfRange, &range) else { return nil }
        AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        guard AXUIElementSetAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, axRange) == .success else { return nil }
        Thread.sleep(forTimeInterval: 0.08)
        var raw: CFTypeRef?
        var got = CFRange(location: -1, length: -1)
        if AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, &raw) == .success, let raw {
            AXValueGetValue(raw as! AXValue, .cfRange, &got)
        }
        guard got.location == start16, got.length == length16 else {
            PastePath.note("  replace: selection did not stick")
            return nil
        }
        let probe = PastePath.probe(text)
        let before = PastePath.hits(probe, in: el)
        if !PastePath.isWebText(el),
           AXUIElementSetAttributeValue(el, kAXSelectedTextAttribute as CFString, text as CFString) == .success {
            Thread.sleep(forTimeInterval: 0.12)
            if PastePath.hits(probe, in: el) > before { return "ax-replace" }
        }
        // WebKit and Electron boxes: Cmd-V over the selection.
        guard PastePath.ensureFront(pid), PastePath.frontPID() == pid, PastePath.commandV() else { return nil }
        for _ in 0..<10 {
            Thread.sleep(forTimeInterval: 0.1)
            if PastePath.hits(probe, in: el) > before { return "cmdv-replace" }
        }
        return nil
    }

    /// Where paisty's earlier draft sits in the reply box (exact, edited or partial), from the bundled helper.
    static func locatePlan(field: String, prior: String) -> [String: Any] {
        let dir = FileManager.default.temporaryDirectory
        let f = dir.appendingPathComponent("paiste-field.txt")
        let p = dir.appendingPathComponent("paiste-prior-locate.txt")
        try? field.data(using: .utf8)?.write(to: f)
        try? prior.data(using: .utf8)?.write(to: p)
        defer {
            try? FileManager.default.removeItem(at: f)
            try? FileManager.default.removeItem(at: p)
        }
        let run = runPaiste(["locate", "--field", f.path, "--prior", p.path], timeout: 10)
        guard run.status == 0, let data = run.out.data(using: .utf8),
              let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return ["mode": "insert"] }
        return doc
    }

    func finish(path: String, note: String = "") {
        stopSpin()
        pulse.send(.finished)
        markHandled(draftSig)
        let copied = path == "copied" || path == "cmdv-unverified" || path == "copied-untrusted"
        // cmdv-unverified: Cmd-V was sent but the field could not be read back. Do not invite a second paste.
        let tip = path == "copied-redraft" ? "Redraft copied — select your earlier draft and press Cmd-V"
            : path == "ax-replace" || path == "cmdv-replace" ? "Replaced your earlier draft"
            : path == "copied-untrusted" ? "Copied. Turn on Accessibility to paste automatically."
            : path == "copied" ? "Copied — click the reply box and press Cmd-V"
            : path == "cmdv-unverified" ? "Pasted? If it is not in the reply box, press Cmd-V" : "Pasted"
        item?.button?.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: tip)
        item?.button?.toolTip = tip
        item?.button?.appearsDisabled = false
        log.error("\(copied ? "copied" : "pasted", privacy: .public) \(path, privacy: .public) \(note, privacy: .public)")
        busy = false
        DispatchQueue.main.asyncAfter(deadline: .now() + (path == "copied-redraft" ? 4 : 1.2)) {
            guard !self.busy else { return }
            self.pulse.send(.settle)
            self.item?.button?.image = self.mark
            self.item?.button?.toolTip = "paisty"
        }
    }

    func fail(_ hint: String) {
        stopSpin()
        pulse.send(.failed)
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
            menu.addItem(header("No drafts yet — left-click the p icon to draft"))
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
        let look = NSMenuItem(title: "Look things up before drafting", action: #selector(toggleLookups(_:)), keyEquivalent: "")
        look.target = self
        look.state = Self.lookupsOn() ? .on : .off
        menu.addItem(look)
        let shot = NSMenuItem(title: "Draft from copied screenshot", action: #selector(draftFromScreenshot), keyEquivalent: "")
        shot.target = self
        shot.isEnabled = !busy && Self.pasteboardImage() != nil
        menu.addItem(shot)
        menu.addItem(.separator())
        let updater = PaistyUpdater.shared
        menu.addItem(header("paisty \(updater.currentVersion) (\(updater.currentBuild))"))
        let check = NSMenuItem(title: "Check for updates", action: #selector(checkUpdates), keyEquivalent: "")
        check.target = self
        menu.addItem(check)
        let autoUpdate = NSMenuItem(title: "Install updates automatically", action: #selector(toggleAutoUpdate(_:)), keyEquivalent: "")
        autoUpdate.target = self
        autoUpdate.state = updater.autoOn ? .on : .off
        menu.addItem(autoUpdate)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit paisty", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        quit.target = NSApp
        menu.addItem(quit)
        return menu
    }

    @objc func checkUpdates() {
        PaistyUpdater.shared.check(userAsked: true)
    }

    @objc func toggleAutoUpdate(_ sender: NSMenuItem) {
        let on = !PaistyUpdater.shared.autoOn
        UserDefaults.standard.set(on, forKey: PaistyUpdater.autoKey)
        sender.state = on ? .on : .off
    }

    static func pasteboardImage() -> Data? {
        let board = NSPasteboard.general
        if let png = board.data(forType: .png) { return png }
        if let tiff = board.data(forType: .tiff), let rep = NSBitmapImageRep(data: tiff) {
            return rep.representation(using: .png, properties: [:])
        }
        if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingContentsConformToTypes: ["public.image"]]) as? [URL],
           let first = urls.first, let data = try? Data(contentsOf: first),
           let rep = NSBitmapImageRep(data: data) {
            return rep.representation(using: .png, properties: [:])
        }
        return nil
    }

    /// A screenshot of a thread on the clipboard in, a ready draft on the clipboard out.
    @objc func draftFromScreenshot() {
        guard !busy, let png = Self.pasteboardImage() else { return }
        busy = true
        startSpin()
        let pid = lastPID
        DispatchQueue.global(qos: .userInitiated).async {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-shot-\(Int(Date().timeIntervalSince1970)).png")
            try? png.write(to: url)
            defer { try? FileManager.default.removeItem(at: url) }
            let run = Self.runPaiste(["shot", "--image", url.path], timeout: 240)
            guard run.status == 0, !run.out.isEmpty else {
                DispatchQueue.main.async { self.fail(run.out == "license" ? "License needed" : "No draft from that screenshot") }
                return
            }
            let path = self.deliver(run.out, pid: pid, field: nil)
            DispatchQueue.main.async { self.finish(path: path, note: "screenshot") }
        }
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

    /// Returns the path that landed, or nil when the field would not take the text. Never presses Return.
    static func insert(_ text: String, into el: AXUIElement, pid: pid_t) -> String? {
        PastePath.insert(text, into: el, pid: pid)
    }

    /// Cmd-V into the frontmost app. Only Cmd-V; no Return.
    static func commandV() -> Bool {
        PastePath.commandV()
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

    static func draft(_ scene: String, prior: String = "", locks: [String: Any]? = nil) -> (reply: String, blocked: String) {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paiste-scene.json")
        try? scene.data(using: .utf8)?.write(to: url)
        let priorURL = FileManager.default.temporaryDirectory.appendingPathComponent("paiste-prior.txt")
        if !prior.isEmpty { try? prior.data(using: .utf8)?.write(to: priorURL) }
        // Pins from the Redraft popup ({"pins": [...]} or {"fresh": true}); the draft step decides the rest.
        let locksURL = FileManager.default.temporaryDirectory.appendingPathComponent("paiste-locks.json")
        var lockArgs: [String] = []
        if !prior.isEmpty, let locks, let data = try? JSONSerialization.data(withJSONObject: locks),
           (try? data.write(to: locksURL)) != nil {
            lockArgs = ["--locks", locksURL.path]
        }
        defer {
            try? FileManager.default.removeItem(at: url)
            try? FileManager.default.removeItem(at: priorURL)
            try? FileManager.default.removeItem(at: locksURL)
        }
        let task = paisteProcess(["draft", "--scene", url.path] + (prior.isEmpty ? [] : ["--prior", priorURL.path]) + lockArgs)
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
        let text = payload(element as! AXUIElement, appName: NSRunningApplication(processIdentifier: pid)?.localizedName ?? "")
        return withWindowText(text, pid: pid)
    }

    /// Mail and chat windows carry pictures and tables that accessibility text leaves out
    /// ("see the asterisk below"). Read the window once with on-device Vision and add it.
    static let ocrApps = ["com.microsoft.outlook", "com.apple.mail", "com.tinyspeck.slackmacgap", "com.microsoft.teams",
                          "us.zoom.xos", "com.readdle.smartemail", "com.superhuman", "com.google.chrome", "com.apple.safari"]

    static func withWindowText(_ scene: String, pid: pid_t) -> String {
        let bundle = (NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? "").lowercased()
        guard ocrApps.contains(where: { bundle.hasPrefix($0) }), CGPreflightScreenCaptureAccess(),
              let data = scene.data(using: .utf8),
              var doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return scene }
        let ocr = windowOCR(pid: pid)
        if ocr.isEmpty { return scene }
        doc["window_ocr"] = String(ocr.prefix(20000))
        guard let out = try? JSONSerialization.data(withJSONObject: doc), let text = String(data: out, encoding: .utf8) else { return scene }
        return text
    }

    static func windowOCR(pid: pid_t) -> String {
        var lines: [String] = []
        let wait = DispatchSemaphore(value: 0)
        Task.detached {
            lines = await readWindow(pid: pid)
            wait.signal()
        }
        if wait.wait(timeout: .now() + 5) == .timedOut { return "" }
        return lines.joined(separator: "\n")
    }

    static func readWindow(pid: pid_t) async -> [String] {
        guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true) else { return [] }
        let windows = content.windows.filter {
            $0.owningApplication?.processID == pid && $0.isOnScreen && $0.frame.width > 240 && $0.frame.height > 160
        }
        guard let window = windows.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }) else { return [] }
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let config = SCStreamConfiguration()
        config.width = max(1, Int(window.frame.width * 2))
        config.height = max(1, Int(window.frame.height * 2))
        config.showsCursor = false
        config.capturesAudio = false
        guard let image = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) else { return [] }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        try? VNImageRequestHandler(cgImage: image).perform([request])
        let items = (request.results ?? []).compactMap { obs -> (CGRect, String)? in
            guard let text = obs.topCandidates(1).first?.string else { return nil }
            return (obs.boundingBox, text)
        }
        // Top to bottom, then left to right; one line per row.
        let sorted = items.sorted { a, b in
            abs(a.0.maxY - b.0.maxY) < 0.006 ? a.0.minX < b.0.minX : a.0.maxY > b.0.maxY
        }
        var out: [String] = []
        var row: [String] = []
        var rowY: CGFloat = -1
        for (box, text) in sorted {
            if rowY >= 0 && abs(box.maxY - rowY) < 0.006 {
                row.append(text)
            } else {
                if !row.isEmpty { out.append(row.joined(separator: "  ")) }
                row = [text]
                rowY = box.maxY
            }
        }
        if !row.isEmpty { out.append(row.joined(separator: "  ")) }
        return out
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
        // `paisty --check-update`: ask the feed exactly as the app does, print the answer, exit. Nothing installs.
        if CommandLine.arguments.contains("--check-update") {
            exit(PaistyUpdater.shared.dryRun())
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.delegate = paisteDelegate
        app.run()
    }
}


