import Cocoa

/// Onboarding window. Local, API, or both. Mail, calendar, notes, and chat stay with the model.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

struct LocalHop {
    var host = ""
    var remote = ""
    var url = ""
    var model = "local"
}

struct ApiHop {
    var url = ""
    var model = ""
    var key = ""
    var hasKey = false
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
    var page = 0
    var loaded = false
    var link = "local"
    var pickedApp = ""
    var showCustom = false
    var locals: [LocalHop] = [LocalHop()]
    var apis: [ApiHop] = [ApiHop()]
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
    var extraHeight: CGFloat = 220

    override init() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 540, height: 760),
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
        let mark = NSImageView()
        mark.image = PaisteApp.logo(64)
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.imageAlignment = .alignCenter
        mark.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(mark)
        NSLayoutConstraint.activate([
            mark.widthAnchor.constraint(equalTo: stack.widthAnchor),
            mark.heightAnchor.constraint(equalToConstant: 64),
        ])
        stack.addArrangedSubview(titleField)
        stack.addArrangedSubview(body)
        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.documentView = extra
        stack.addArrangedSubview(scroll)
        stack.addArrangedSubview(statusField)
        stack.addArrangedSubview(footer)
        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 8
        buttons.addArrangedSubview(backButton)
        buttons.addArrangedSubview(nextButton)
        stack.addArrangedSubview(buttons)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -16),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(equalToConstant: 320),
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
        if !["local", "api", "both", "app"].contains(link) { link = "local" }
        pickedApp = doc["app"] as? String ?? ""
        if !["claude", "codex", "cursor"].contains(pickedApp) { pickedApp = "" }
        showCustom = false
        if let rows = doc["locals"] as? [[String: Any]], !rows.isEmpty {
            locals = rows.map { row in
                LocalHop(
                    host: row["host"] as? String ?? "",
                    remote: row["remote"] as? String ?? "",
                    url: row["url"] as? String ?? "",
                    model: row["model"] as? String ?? "local"
                )
            }
        } else {
            locals = [parsedLocal(doc["local"] as? String ?? "", doc["local_model"] as? String ?? "local")]
        }
        if let rows = doc["apis"] as? [[String: Any]], !rows.isEmpty {
            apis = rows.map { row in
                ApiHop(
                    url: row["url"] as? String ?? "",
                    model: row["model"] as? String ?? "",
                    key: "",
                    hasKey: (row["has_key"] as? Bool) ?? false
                )
            }
        } else if let url = doc["api_url"] as? String, !url.isEmpty {
            apis = [ApiHop(url: url, model: doc["api_model"] as? String ?? "", key: "", hasKey: (doc["has_key"] as? Bool) ?? false)]
        }
        if locals.isEmpty { locals = [LocalHop()] }
        if apis.isEmpty { apis = [ApiHop()] }
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
        if fields["host-0"] != nil || fields["url-0"] != nil || fields["remote-0"] != nil {
            var next: [LocalHop] = []
            for i in 0..<locals.count {
                guard fields["model-\(i)"] != nil || fields["host-\(i)"] != nil else { continue }
                next.append(LocalHop(
                    host: fields["host-\(i)"]?.stringValue ?? "",
                    remote: fields["remote-\(i)"]?.stringValue ?? "",
                    url: fields["url-\(i)"]?.stringValue ?? "",
                    model: fields["model-\(i)"]?.stringValue ?? "local"
                ))
            }
            if !next.isEmpty { locals = next }
        }
        if fields["api-url-0"] != nil {
            var next: [ApiHop] = []
            for i in 0..<apis.count {
                guard fields["api-url-\(i)"] != nil else { continue }
                var row = i < apis.count ? apis[i] : ApiHop()
                row.url = fields["api-url-\(i)"]?.stringValue ?? ""
                row.model = fields["api-model-\(i)"]?.stringValue ?? ""
                if let field = fields["api-key-\(i)"], !field.stringValue.isEmpty {
                    row.key = field.stringValue
                }
                next.append(row)
            }
            if !next.isEmpty { apis = next }
        }
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
        statusField.stringValue = "Local needs a model already running on this Mac, or on a powerful machine you can ssh to. Host name, then http://127.0.0.1:8080/v1. Paid works anywhere online: the service URL, the model name, and the key. Press Connect. The key stays here."
    }

    func save() {
        var localRows: [[String: String]] = []
        for row in locals {
            let host = row.host.trimmingCharacters(in: .whitespacesAndNewlines)
            let remote = row.remote.trimmingCharacters(in: .whitespacesAndNewlines)
            let url = row.url.trimmingCharacters(in: .whitespacesAndNewlines)
            if host.isEmpty && url.isEmpty { continue }
            localRows.append([
                "host": host,
                "remote": remote,
                "url": url,
                "model": row.model.trimmingCharacters(in: .whitespacesAndNewlines),
            ])
        }
        var apiRows: [[String: String]] = []
        var keyed: [Int] = []
        for (index, row) in apis.enumerated() {
            let url = row.url.trimmingCharacters(in: .whitespacesAndNewlines)
            if url.isEmpty { continue }
            var item = ["url": url, "model": row.model.trimmingCharacters(in: .whitespacesAndNewlines)]
            if !row.key.isEmpty {
                item["key"] = row.key
                keyed.append(index)
            }
            apiRows.append(item)
        }
        let hasLocal = !localRows.isEmpty
        let hasApi = !apiRows.isEmpty
        let mode = !hasLocal && !hasApi && !pickedApp.isEmpty ? "app" : (hasApi ? "both" : "local")
        var doc: [String: Any] = [
            "link": mode,
            "locals": localRows,
            "apis": apiRows,
            "mail": mailFront ? "front" : "off",
            "chat": chatFront ? "front" : "off",
            "notes": notesOn ? "folder" : "off",
            "notes_path": notesPath,
            "calendar": calendarOn ? "folder" : "off",
            "calendar_path": calendarPath,
        ]
        if !pickedApp.isEmpty { doc["app"] = pickedApp }
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
        let wroteKey = !keyed.isEmpty
        for index in apis.indices { apis[index].key = "" }
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
                    if wroteKey {
                        for index in keyed { self.apis[index].hasKey = true }
                    }
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
            titleField.stringValue = "Step 1. Connect your systems."
            body.stringValue = "Three connections, then the first click.\n\n1. Connect your AI.\n2. Connect mail and calendar inside that AI.\n3. Connect notes or Obsidian inside that AI.\n\npaisty does not sign into those systems and does not keep them. The click asks your AI, which already has them, and copies a reply.\n\nPaste the key from the email once."
            _ = field("license", "License key from the email", "", 8)
            let saveKey = NSButton(title: "Save key", target: self, action: #selector(saveLicense))
            saveKey.frame = NSRect(x: 0, y: 44, width: 140, height: 28)
            saveKey.bezelStyle = .rounded
            extra.addSubview(saveKey)
            extraHeight = 84
        case 1:
            titleField.stringValue = "1. Connect your AI."
            body.stringValue = "One click checks the app and the command on this Mac. If neither is signed in, the browser opens and signs this Mac into that account. Later clicks ask this AI, including the mail, calendar, and notes you connect inside it."
            modelPage()
        case 2:
            titleField.stringValue = "2. Mail and calendar, inside your AI."
            body.stringValue = "In Claude, Cursor, or Codex, connect the mail and calendar you already use. paisty does not sign in and does not keep the message. The switch below only lets a click read the window that is already open."
            toggle("mail", "Read the mail window once", mailFront, 8)
            extraHeight = 40
        case 3:
            titleField.stringValue = "2. Calendar, still inside your AI."
            body.stringValue = "Connect the calendar in the same AI. paisty does not accept or send invites, and it does not keep events. A folder of .ics files is optional. It is read on the click, then forgotten."
            toggle("calendar", "Read a folder of .ics files on the click", calendarOn, 8)
            _ = field("calendar_path", "Folder of .ics files", calendarPath, 40)
            extraHeight = 100
        case 4:
            titleField.stringValue = "3. Notes and Obsidian, inside your AI."
            body.stringValue = notesCopy()
            toggle("notes", "Read a notes folder on the click", notesOn, 8)
            _ = field("notes_path", "Notes folder", notesPath.isEmpty ? obsidian : notesPath, 40)
            extraHeight = 100
        case 5:
            titleField.stringValue = "3. Chat, inside your AI."
            body.stringValue = "Connect chat in the same AI. paisty does not join the chat and does not keep it. The switch only reads the window in front for that one click."
            toggle("chat", "Read the chat window once", chatFront, 8)
            extraHeight = 40
        default:
            titleField.stringValue = "Nothing is kept."
            body.stringValue = reviewCopy()
            extraHeight = 8
        }
        extra.frame = NSRect(x: 0, y: 0, width: 484, height: max(extraHeight, 40))
    }

    func notesCopy() -> String {
        var text = "Give your AI the notes folder or the Obsidian vault. Obsidian, Apple Notes, or a project file. paisty does not upload it and does not keep it. If a notes window is in front, the click can read it once."
        if !obsidian.isEmpty {
            text += "\n\nA vault is on this Mac at " + obsidian + ". Give that folder to your model. paisty does not upload it."
        }
        return text
    }

    func reviewCopy() -> String {
        let localCount = locals.filter { !$0.host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !$0.url.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.count
        let remoteCount = locals.filter { !$0.host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.count
        let apiCount = apis.filter { !$0.url.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.count
        let model: String
        if !pickedApp.isEmpty {
            let name = ["claude": "Claude", "codex": "Codex", "cursor": "Cursor"][pickedApp] ?? pickedApp
            model = "\(localCount) local first. If that does not answer, " + name + "."
        } else if link == "both" {
            model = "\(localCount) local, \(remoteCount) on another Mac, then \(apiCount) paid."
        } else if link == "api" {
            model = "\(apiCount) paid."
        } else {
            model = "\(localCount) local, \(remoteCount) on another Mac."
        }
        let mail = mailFront ? "Mail window, once." : "Mail stays with your model."
        let chat = chatFront ? "Chat window, once." : "Chat stays with your model."
        let notes = notesOn ? "A notes folder, read on the click." : "Notes stay with your model."
        let calendar = calendarOn ? "A calendar folder, read on the click." : "Calendar stays with your model."
        return model + "\n" + mail + " " + chat + "\n" + notes + " " + calendar + "\n\nA local model has to already be running on this Mac or on a powerful machine you can reach. A paid model works anywhere this Mac is online.\n\nSaving writes the model choice on this Mac. It does not connect paisty to mail, calendar, notes, or chat, and it does not retain any data."
    }

    func modelPage() {
        var y = caption("One click. A login already on this Mac is used. Otherwise the browser opens.", 0)
        y = choiceButton("Connect Claude", "claude", y)
        y = choiceButton("Connect Codex", "codex", y)
        y = choiceButton("Connect Cursor", "cursor", y)
        y = choiceButton("Custom", "custom", y)
        y = caption("Tried first, when a local model is already running. The login above is the fallback.", y)
        for (i, row) in locals.enumerated() {
            y = field("host-\(i)", "ssh host name. Blank if the model is on this Mac.", row.host, y)
            y = field("remote-\(i)", "Address on that Mac. http://127.0.0.1:8080/v1", row.remote, y)
            y = field("url-\(i)", "Or on this Mac. http://127.0.0.1:11434/v1", row.url, y)
            y = field("model-\(i)", "Model name", row.model, y)
            y = rowButtons(probe: 1000 + i, remove: 3000 + i, showRemove: locals.count > 1, y)
        }
        if locals.count < 4 {
            y = addButton("Add a local model", #selector(addLocal), y)
        }
        if !showCustom {
            extraHeight = y + 8
            return
        }
        if true {
            y = caption("Paid works anywhere this Mac is online. OpenAI-style /v1. Tried after the local rows.", y)
            for (i, row) in apis.enumerated() {
                y = field("api-url-\(i)", "https://api.openai.com/v1", row.url, y)
                y = field("api-model-\(i)", "Model name", row.model, y)
                let placeholder = row.hasKey || !row.key.isEmpty ? "Key already on this Mac. Leave blank to keep it." : "API key. Stays on this Mac."
                y = field("api-key-\(i)", placeholder, "", y, secure: true)
                y = rowButtons(probe: 2000 + i, remove: 4000 + i, showRemove: apis.count > 1, y)
            }
            if apis.count < 4 {
                y = addButton("Add a paid model", #selector(addPaid), y)
            }
        }
        let help = NSButton(title: "Help me point my model", target: self, action: #selector(hint))
        help.frame = NSRect(x: 0, y: y, width: 220, height: 28)
        help.bezelStyle = .rounded
        extra.addSubview(help)
        extraHeight = y + 36
    }

    @discardableResult
    func choiceButton(_ title: String, _ name: String, _ y: CGFloat) -> CGFloat {
        var label = title
        if name == "custom" && showCustom {
            label = "Custom"
        } else if pickedApp == name {
            label = title.replacingOccurrences(of: "Connect ", with: "") + ", connected"
        }
        let button = NSButton(title: label, target: self, action: #selector(pickApp(_:)))
        button.identifier = NSUserInterfaceItemIdentifier(name)
        button.frame = NSRect(x: 0, y: y, width: 460, height: 36)
        button.bezelStyle = .rounded
        extra.addSubview(button)
        return y + 44
    }

    @objc func pickApp(_ sender: NSButton) {
        let name = sender.identifier?.rawValue ?? ""
        if name == "custom" {
            capture()
            showCustom = true
            render()
            return
        }
        guard ["claude", "codex", "cursor"].contains(name) else { return }
        statusField.stringValue = "Checking the login on this Mac."
        DispatchQueue.global(qos: .userInitiated).async {
            let done = SetupBoard.run(["onboard", "--connect", name])
            let word = done.out.trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                if word == "signed-in" {
                    let saved = SetupBoard.run(["onboard", "--use", name])
                    let wrote = saved.out.trimmingCharacters(in: .whitespacesAndNewlines)
                    if wrote == "using" {
                        self.pickedApp = name
                        self.showCustom = false
                        let label = ["claude": "Claude", "codex": "Codex", "cursor": "Cursor"][name] ?? name
                        self.statusField.stringValue = "A click will use " + label + "."
                        self.render()
                    } else {
                        self.statusField.stringValue = "That login was not written."
                    }
                } else if word == "missing" {
                    self.openBrowser(name)
                    self.statusField.stringValue = "The browser is open. Sign in, install the app, then press the button again."
                } else {
                    _ = SetupBoard.run(["onboard", "--login", name])
                    self.statusField.stringValue = "The browser is open. Finish signing this Mac in, then press the button again."
                }
            }
        }
    }

    func openBrowser(_ name: String) {
        let pages = [
            "claude": "https://claude.ai/login",
            "codex": "https://chatgpt.com/",
            "cursor": "https://cursor.com/login",
        ]
        guard let raw = pages[name], let url = URL(string: raw) else { return }
        NSWorkspace.shared.open(url)
    }

    @objc func addLocal() {
        capture()
        if locals.count < 4 { locals.append(LocalHop()) }
        render()
    }

    @objc func addPaid() {
        capture()
        if apis.count < 4 { apis.append(ApiHop()) }
        render()
    }

    @objc func removeRow(_ sender: NSButton) {
        capture()
        if sender.tag >= 4000 {
            let index = sender.tag - 4000
            if apis.count > 1, apis.indices.contains(index) { apis.remove(at: index) }
        } else if sender.tag >= 3000 {
            let index = sender.tag - 3000
            if locals.count > 1, locals.indices.contains(index) { locals.remove(at: index) }
        }
        render()
    }

    @objc func probeRow(_ sender: NSButton) {
        capture()
        let tag = sender.tag
        var line = ""
        var key = ""
        if tag >= 2000 {
            let index = tag - 2000
            guard apis.indices.contains(index) else { return }
            line = apis[index].url.trimmingCharacters(in: .whitespacesAndNewlines)
            key = apis[index].key
        } else {
            let index = tag - 1000
            guard locals.indices.contains(index) else { return }
            line = localLine(locals[index])
        }
        guard !line.isEmpty else {
            statusField.stringValue = "Add the address first."
            return
        }
        statusField.stringValue = "Checking that address. Nothing from the window is sent."
        let payload: [String: String] = ["line": line, "key": key]
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-probe.json")
        do {
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            statusField.stringValue = "That address was not checked."
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let done = SetupBoard.run(["onboard", "--probe", "--answers", url.path])
            try? FileManager.default.removeItem(at: url)
            let text = done.out.trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                self.statusField.stringValue = text.isEmpty ? "Not reachable." : String(text.prefix(200))
            }
        }
    }

    func localLine(_ row: LocalHop) -> String {
        let host = row.host.trimmingCharacters(in: .whitespacesAndNewlines)
        if !host.isEmpty {
            var remote = row.remote.trimmingCharacters(in: .whitespacesAndNewlines)
            if remote.isEmpty { remote = "http://127.0.0.1:8080/v1" }
            return "ssh \(host) \(remote)"
        }
        return row.url.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func parsedLocal(_ line: String, _ model: String) -> LocalHop {
        let parts = line.split(separator: " ")
        if parts.count >= 3, parts[0] == "ssh" {
            return LocalHop(host: String(parts[1]), remote: String(parts[2]), url: "", model: model)
        }
        if line.isEmpty { return LocalHop() }
        return LocalHop(url: line, model: model)
    }

    @discardableResult
    func caption(_ text: String, _ y: CGFloat) -> CGFloat {
        let label = NSTextField(labelWithString: text)
        label.frame = NSRect(x: 0, y: y, width: 460, height: 32)
        label.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        label.textColor = NSColor(srgbRed: 0.25, green: 0.22, blue: 0.18, alpha: 1)
        label.maximumNumberOfLines = 2
        extra.addSubview(label)
        return y + 36
    }

    @discardableResult
    func rowButtons(probe: Int, remove: Int, showRemove: Bool, _ y: CGFloat) -> CGFloat {
        let connect = NSButton(title: "Connect", target: self, action: #selector(probeRow(_:)))
        connect.tag = probe
        connect.frame = NSRect(x: 0, y: y, width: 110, height: 26)
        connect.bezelStyle = .rounded
        extra.addSubview(connect)
        if showRemove {
            let cut = NSButton(title: "Remove", target: self, action: #selector(removeRow(_:)))
            cut.tag = remove
            cut.frame = NSRect(x: 118, y: y, width: 90, height: 26)
            cut.bezelStyle = .rounded
            extra.addSubview(cut)
        }
        return y + 34
    }

    @discardableResult
    func addButton(_ title: String, _ action: Selector, _ y: CGFloat) -> CGFloat {
        let button = NSButton(title: title, target: self, action: action)
        button.frame = NSRect(x: 0, y: y, width: 180, height: 26)
        button.bezelStyle = .rounded
        extra.addSubview(button)
        return y + 34
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
        let script = paisteCommand()
        if script == "hailports" {
            task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            task.arguments = ["hailports", "clip"] + args
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
