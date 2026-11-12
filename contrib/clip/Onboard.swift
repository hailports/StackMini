import Cocoa

/// Onboarding window. Local, API, or both. Mail, calendar, notes, and chat stay with the model.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

final class SetupBoard: NSObject, NSWindowDelegate {
    let window: NSWindow
    let titleField = NSTextField(labelWithString: "")
    let body = NSTextField(wrappingLabelWithString: "")
    let extra = FlippedView()
    let footer = NSTextField(wrappingLabelWithString: "paisty does not connect to your systems or retain any data. That is your model's job.")
    let statusField = NSTextField(wrappingLabelWithString: "")
    let backButton = NSButton(title: "Back", target: nil, action: nil)
    let nextButton = NSButton(title: "Next", target: nil, action: nil)
    var extraHeight: NSLayoutConstraint?
    var page = 0
    var loaded = false
    var link = "local"
    var local = ""
    var localModel = "local"
    var apiURL = ""
    var apiModel = ""
    var apiKey = ""
    var hasKey = false
    var mailFront = true
    var chatFront = true
    var notesOn = false
    var notesPath = ""
    var calendarOn = false
    var calendarPath = ""
    var obsidian = ""
    var linkControl: NSSegmentedControl?
    var fields: [String: NSTextField] = [:]
    var toggles: [String: NSButton] = [:]

    override init() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 540, height: 660),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "paisty"
        window.isReleasedWhenClosed = false
        super.init()
        window.delegate = self
        backButton.target = self
        backButton.action = #selector(back)
        nextButton.target = self
        nextButton.action = #selector(forward)
        buildChrome()
        render()
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    func buildChrome() {
        guard let content = window.contentView else { return }
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor(srgbRed: 0.953, green: 0.937, blue: 0.902, alpha: 1).cgColor
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        titleField.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        titleField.textColor = NSColor(srgbRed: 0.16, green: 0.13, blue: 0.10, alpha: 1)
        body.font = NSFont.systemFont(ofSize: 13)
        body.preferredMaxLayoutWidth = 480
        body.maximumNumberOfLines = 8
        footer.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        footer.textColor = NSColor(srgbRed: 0.45, green: 0.28, blue: 0.16, alpha: 1)
        footer.preferredMaxLayoutWidth = 480
        footer.maximumNumberOfLines = 3
        statusField.font = NSFont.systemFont(ofSize: 12)
        statusField.textColor = NSColor(srgbRed: 0.25, green: 0.22, blue: 0.18, alpha: 1)
        statusField.preferredMaxLayoutWidth = 480
        extra.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        stack.addArrangedSubview(titleField)
        stack.addArrangedSubview(body)
        stack.addArrangedSubview(extra)
        stack.addArrangedSubview(statusField)
        stack.addArrangedSubview(footer)
        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 8
        buttons.addArrangedSubview(backButton)
        buttons.addArrangedSubview(nextButton)
        stack.addArrangedSubview(buttons)
        let height = extra.heightAnchor.constraint(equalToConstant: 220)
        extraHeight = height
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            extra.widthAnchor.constraint(equalTo: stack.widthAnchor),
            height,
        ])
        window.center()
    }

    func show() {
        render()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if !loaded {
            loaded = true
            load()
        }
    }

    func load() {
        DispatchQueue.global(qos: .userInitiated).async {
            let text = SetupBoard.run(["onboard", "--show"]).out
            guard let data = text.data(using: .utf8),
                  let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                self.applyShow(doc)
                self.render()
            }
        }
    }

    func applyShow(_ doc: [String: Any]) {
        link = (doc["link"] as? String) ?? "local"
        if !["local", "api", "both"].contains(link) { link = "local" }
        local = doc["local"] as? String ?? ""
        localModel = doc["local_model"] as? String ?? "local"
        apiURL = doc["api_url"] as? String ?? ""
        apiModel = doc["api_model"] as? String ?? ""
        hasKey = (doc["has_key"] as? Bool) ?? false
        mailFront = (doc["mail"] as? String) != "off"
        chatFront = (doc["chat"] as? String) != "off"
        notesOn = (doc["notes"] as? String) == "folder"
        notesPath = doc["notes_path"] as? String ?? ""
        calendarOn = (doc["calendar"] as? String) == "folder"
        calendarPath = doc["calendar_path"] as? String ?? ""
        obsidian = doc["obsidian"] as? String ?? ""
    }

    func capture() {
        if let linkControl {
            let names = ["local", "api", "both"]
            let index = linkControl.selectedSegment
            if names.indices.contains(index) { link = names[index] }
        }
        if let field = fields["local"] { local = field.stringValue }
        if let field = fields["local_model"] { localModel = field.stringValue }
        if let field = fields["api_url"] { apiURL = field.stringValue }
        if let field = fields["api_model"] { apiModel = field.stringValue }
        if let field = fields["api_key"], !field.stringValue.isEmpty { apiKey = field.stringValue }
        if let field = fields["notes_path"] { notesPath = field.stringValue }
        if let field = fields["calendar_path"] { calendarPath = field.stringValue }
        if let box = toggles["mail"] { mailFront = box.state == .on }
        if let box = toggles["chat"] { chatFront = box.state == .on }
        if let box = toggles["notes"] { notesOn = box.state == .on }
        if let box = toggles["calendar"] { calendarOn = box.state == .on }
    }

    @objc func back() {
        capture()
        if page > 0 { page -= 1 }
        statusField.stringValue = ""
        render()
    }

    @objc func forward() {
        capture()
        if page < 6 {
            page += 1
            statusField.stringValue = ""
            render()
            return
        }
        save()
    }

    @objc func saveLicense() {
        capture()
        let key = (fields["license"]?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard key.contains("."), key.count > 40, key.count < 4000 else {
            statusField.stringValue = "That key was not kept."
            return
        }
        let dir = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".config/paiste")
        let url = dir.appendingPathComponent("license")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try key.write(to: url, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            statusField.stringValue = "Key saved on this Mac."
        } catch {
            statusField.stringValue = "That key was not kept."
        }
    }

    @objc func hint() {
        capture()
        statusField.stringValue = "Asking the setup guide. No mail, calendar, or notes are sent."
        DispatchQueue.global(qos: .userInitiated).async {
            let text = SetupBoard.run(
                ["onboard", "--hint"],
                env: ["PAISTY_SETUP_URL": "https://paisty.com/api/setup"]
            ).out.trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                self.statusField.stringValue = text.isEmpty
                    ? "The guide is not reachable. Your model stays on this Mac. Nothing was sent."
                    : String(text.prefix(500))
            }
        }
    }

    func save() {
        var doc: [String: Any] = [
            "link": link,
            "local": local,
            "local_model": localModel,
            "api_url": apiURL,
            "api_model": apiModel,
            "mail": mailFront ? "front" : "off",
            "chat": chatFront ? "front" : "off",
            "notes": notesOn ? "folder" : "off",
            "notes_path": notesPath,
            "calendar": calendarOn ? "folder" : "off",
            "calendar_path": calendarPath,
        ]
        if !apiKey.isEmpty { doc["api_key"] = apiKey }
        guard let data = try? JSONSerialization.data(withJSONObject: doc) else {
            statusField.stringValue = "Could not save."
            return
        }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-onboard.json")
        do {
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            statusField.stringValue = "Could not save."
            return
        }
        let wroteKey = !apiKey.isEmpty
        apiKey = ""
        nextButton.isEnabled = false
        statusField.stringValue = "Saving on this Mac."
        let route = ProcessInfo.processInfo.environment["PAISTE_ROUTE"] ?? ""
        DispatchQueue.global(qos: .userInitiated).async {
            var args = ["onboard", "--answers", url.path]
            if !route.isEmpty { args += ["--route-out", route] }
            let done = SetupBoard.run(args)
            try? FileManager.default.removeItem(at: url)
            let line = done.out.trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                self.nextButton.isEnabled = true
                if done.code == 0 {
                    if wroteKey { self.hasKey = true }
                    self.statusField.stringValue = "Saved on this Mac. Nothing was sent. paisty did not connect to your systems, and it did not keep the window."
                } else {
                    let reason = line.split(separator: "\n").first.map(String.init) ?? "That was not saved."
                    self.statusField.stringValue = reason
                }
            }
        }
    }

    func render() {
        fields = [:]
        toggles = [:]
        linkControl = nil
        extra.subviews.forEach { $0.removeFromSuperview() }
        backButton.isHidden = page == 0
        nextButton.title = page == 6 ? "Save on this Mac" : "Next"
        switch page {
        case 0:
            titleField.stringValue = "One click. The reply is yours."
            body.stringValue = "If your AI is integrated with your email, chat, or notes, paisty works for you.\n\npaisty does not connect to those systems and does not retain any data. That is your model's job. A click reads the window, asks your model, and copies a reply. Then it forgets the window.\n\nAfter you pay, the key arrives by email. Paste it here once."
            _ = field("license", "License key from the email", "", 8)
            let saveKey = NSButton(title: "Save key", target: self, action: #selector(saveLicense))
            saveKey.frame = NSRect(x: 0, y: 44, width: 140, height: 28)
            saveKey.bezelStyle = .rounded
            extra.addSubview(saveKey)
            extraHeight?.constant = 84
        case 1:
            titleField.stringValue = "Your model. Local, API, or both."
            body.stringValue = "This is the only connection paisty makes, and only for that click. Local is a model on this Mac, or ssh to a computer you own. API is a service you already use. Both tries local first, then the API. The key stays on this Mac. It is not sent to paisty."
            modelPage()
        case 2:
            titleField.stringValue = "Mail is your model's job."
            body.stringValue = "Connect mail in the model you chose. paisty will not sign in, and it will not keep the message. The switch only reads the window in front for that one click."
            toggle("mail", "Read the mail window once", mailFront, 8)
            extraHeight?.constant = 40
        case 3:
            titleField.stringValue = "Calendar is your model's job."
            body.stringValue = "Connect the calendar in your model. paisty does not accept or send invites, and it does not keep events. A folder of .ics files is optional. It is read on the click, then forgotten."
            toggle("calendar", "Read a folder of .ics files on the click", calendarOn, 8)
            _ = field("calendar_path", "Folder of .ics files", calendarPath, 40)
            extraHeight?.constant = 100
        case 4:
            titleField.stringValue = "Notes are your model's job."
            body.stringValue = notesCopy()
            toggle("notes", "Read a notes folder on the click", notesOn, 8)
            _ = field("notes_path", "Notes folder", notesPath.isEmpty ? obsidian : notesPath, 40)
            extraHeight?.constant = 100
        case 5:
            titleField.stringValue = "Chat is your model's job."
            body.stringValue = "Connect chat in your model. paisty does not join the chat and does not keep it. The switch only reads the window in front for that one click."
            toggle("chat", "Read the chat window once", chatFront, 8)
            extraHeight?.constant = 40
        default:
            titleField.stringValue = "Nothing is kept."
            body.stringValue = reviewCopy()
            extraHeight?.constant = 8
        }
    }

    func notesCopy() -> String {
        var text = "Connect notes in your model. Obsidian, Apple Notes, or a project file. paisty does not keep them. If a notes window is in front, the click can read it once."
        if !obsidian.isEmpty {
            text += "\n\nA vault is on this Mac at " + obsidian + ". Give that folder to your model. paisty does not upload it."
        }
        return text
    }

    func reviewCopy() -> String {
        let model: String
        if link == "both" {
            model = "Local first, then the API."
        } else if link == "api" {
            model = "API only."
        } else {
            model = "Local only."
        }
        let mail = mailFront ? "Mail window, once." : "Mail stays with your model."
        let chat = chatFront ? "Chat window, once." : "Chat stays with your model."
        let notes = notesOn ? "A notes folder, read on the click." : "Notes stay with your model."
        let calendar = calendarOn ? "A calendar folder, read on the click." : "Calendar stays with your model."
        return model + "\n" + mail + " " + chat + "\n" + notes + " " + calendar + "\n\nSaving writes the model choice on this Mac. It does not connect paisty to mail, calendar, notes, or chat, and it does not retain any data."
    }

    func modelPage() {
        let segment = NSSegmentedControl(labels: ["Local", "API", "Both"], trackingMode: .selectOne, target: self, action: #selector(pickLink))
        segment.frame = NSRect(x: 0, y: 0, width: 280, height: 28)
        let index = ["local": 0, "api": 1, "both": 2][link] ?? 0
        segment.selectedSegment = index
        extra.addSubview(segment)
        linkControl = segment
        var y: CGFloat = 40
        if link != "api" {
            y = field("local", "http://127.0.0.1:8080/v1  or  ssh host http://127.0.0.1:8080/v1", local, y)
            y = field("local_model", "Local model name", localModel, y)
        }
        if link != "local" {
            y = field("api_url", "https://api.example.com/v1", apiURL, y)
            y = field("api_model", "API model name", apiModel, y)
            let placeholder = hasKey || !apiKey.isEmpty ? "Key already on this Mac. Leave blank to keep it." : "API key. Stays on this Mac."
            y = field("api_key", placeholder, "", y, secure: true)
        }
        let help = NSButton(title: "Help me point my model", target: self, action: #selector(hint))
        help.frame = NSRect(x: 0, y: y, width: 220, height: 28)
        help.bezelStyle = .rounded
        extra.addSubview(help)
        extraHeight?.constant = y + 36
    }

    @objc func pickLink() {
        capture()
        render()
    }

    func toggle(_ key: String, _ title: String, _ on: Bool, _ y: CGFloat) {
        let box = NSButton(checkboxWithTitle: title, target: nil, action: nil)
        box.frame = NSRect(x: 0, y: y, width: 460, height: 22)
        box.state = on ? .on : .off
        toggles[key] = box
        extra.addSubview(box)
    }

    @discardableResult
    func field(_ key: String, _ placeholder: String, _ value: String, _ y: CGFloat, secure: Bool = false) -> CGFloat {
        let input: NSTextField = secure
            ? NSSecureTextField(frame: NSRect(x: 0, y: y, width: 480, height: 24))
            : NSTextField(frame: NSRect(x: 0, y: y, width: 480, height: 24))
        input.placeholderString = placeholder
        if !secure { input.stringValue = value }
        fields[key] = input
        extra.addSubview(input)
        return y + 32
    }

    struct Done {
        var out = ""
        var code: Int32 = 1
    }

    static func run(_ args: [String], env extra: [String: String] = [:]) -> Done {
        let task = Process()
        let script = ProcessInfo.processInfo.environment["PAISTE_CMD"] ?? "hailports"
        if script == "hailports" {
            task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            task.arguments = ["hailports", "paiste"] + args
        } else {
            task.executableURL = URL(fileURLWithPath: script)
            task.arguments = args
        }
        var env = ProcessInfo.processInfo.environment
        for (key, value) in extra { env[key] = value }
        task.environment = env
        let out = Pipe()
        let err = Pipe()
        task.standardOutput = out
        task.standardError = err
        do { try task.run() } catch { return Done() }
        task.waitUntilExit()
        _ = err.fileHandleForReading.readDataToEndOfFile()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        return Done(out: String(data: data, encoding: .utf8) ?? "", code: task.terminationStatus)
    }
}
