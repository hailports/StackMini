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
    var foundModels: [[String: String]] = []
    var foundVaults: [String] = []
    var mailApps: [String] = []
    var chatApps: [String] = []
    var littlebirdOn = false
    var scanning = true
    var scanned = false
    var showManualLocal = false
    var showLocalAssist = false
    var feedNote = ""
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
        window.appearance = NSAppearance(named: .aqua)
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
        content.appearance = NSAppearance(named: .aqua)
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
        body.textColor = NSColor(srgbRed: 0.16, green: 0.13, blue: 0.10, alpha: 1)
        body.preferredMaxLayoutWidth = 480
        body.maximumNumberOfLines = 12
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
            DispatchQueue.main.async {
                if let data = text.data(using: .utf8),
                   let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.applyShow(doc)
                    self.render()
                }
            }
            let scanText = SetupBoard.run(["onboard", "--scan"]).out
            DispatchQueue.main.async {
                if let data = scanText.data(using: .utf8),
                   let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.applyScan(doc)
                } else {
                    self.scanning = false
                    self.scanned = true
                }
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
                let previous = i < locals.count ? locals[i].model : "local"
                next.append(LocalHop(
                    host: fields["host-\(i)"]?.stringValue ?? "",
                    remote: fields["remote-\(i)"]?.stringValue ?? "",
                    url: fields["url-\(i)"]?.stringValue ?? "",
                    model: fields["model-\(i)"]?.stringValue ?? previous
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
        if page < 3 {
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
        let mode: String
        if !pickedApp.isEmpty && !hasApi {
            mode = "app"
        } else if hasApi && hasLocal {
            mode = "both"
        } else if hasApi {
            mode = "api"
        } else {
            mode = "local"
        }
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
        nextButton.title = page == 3 ? "Save on this Mac" : "Next"
        switch page {
        case 0:
            titleField.stringValue = "Already on this Mac."
            body.stringValue = "Mail, chat, Littlebird, and an Obsidian vault are turned on when they are already here. A click reads the open window. paisty does not sign in."
            foundPage()
        case 1:
            titleField.stringValue = "Connect your AI to paisty."
            body.stringValue = "One click checks the app on this Mac. If it is not signed in, the browser opens and signs this Mac into that account. A local model is uncommon. The help for that is on this screen."
            aiPage()
        case 2:
            titleField.stringValue = "A local model is optional."
            body.stringValue = "Most people already finished on the last screen. Next keeps that AI. This page is only if you still want a local model."
            localPage()
        default:
            titleField.stringValue = "Save on this Mac."
            body.stringValue = reviewCopy()
            _ = field("license", "License key from the email", "", 8)
            let saveKey = NSButton(title: "Save key", target: self, action: #selector(saveLicense))
            saveKey.frame = NSRect(x: 0, y: 44, width: 140, height: 28)
            saveKey.bezelStyle = .rounded
            extra.addSubview(saveKey)
            extraHeight = 84
        }
        if !body.stringValue.contains("does not keep any data") {
            body.stringValue += "\n\npaisty does not keep any data."
        }
        extra.frame = NSRect(x: 0, y: 0, width: 484, height: max(extraHeight, 40))
    }

    func notesCopy() -> String {
        if foundVaults.isEmpty && obsidian.isEmpty {
            return "No notes folder yet. One click asks the setup assist, which is not your Cursor key and is not stored in the app. It writes a starter from the mail and calendar you just connected. A click then searches that folder."
        }
        return "A notes folder is already on this Mac. One click uses it and remembers the mail and calendar from the last steps."
    }

    func reviewCopy() -> String {
        let localCount = locals.filter { !$0.host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !$0.url.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.count
        let remoteCount = locals.filter { !$0.host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.count
        let apiCount = apis.filter { !$0.url.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.count
        let model: String
        if !pickedApp.isEmpty {
            let name = ["claude": "Claude", "codex": "Codex", "cursor": "Cursor"][pickedApp] ?? pickedApp
            if localCount == 0 {
                model = name + " is the model a click asks. A local model is optional and was not added."
            } else {
                model = name + " is the model a click asks. \(localCount) local " + (localCount == 1 ? "model is" : "models are") + " optional and run after " + name + "."
            }
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
        let known = establishedLocal()
        let shown = known.isEmpty ? model : known + ". " + model
        return "A click asks your AI to search all connected mail, calendar, and notes.\n" + shown + "\n" + mail + " " + chat + "\n" + notes + " " + calendar + "\n\nA local model is optional. The AI login works without one.\n\nSaving writes the choice on this Mac. paisty does not sign into mail, calendar, or notes, and it does not keep them."
    }

    func mailPage() {
        var y = CGFloat(0)
        if !mailApps.isEmpty {
            y = caption("On this Mac: " + mailApps.joined(separator: ", ") + ".", y)
        } else if scanned {
            y = caption("No Mail, Outlook, or Spark app turned up. The buttons still open your AI.", y)
        } else {
            y = caption("Looking at this Mac.", y)
        }
        y = linkButton("Connect mail in Claude", "https://claude.ai/settings/connectors", y)
        y = linkButton("Connect mail in ChatGPT", "https://chatgpt.com/settings/connectors", y)
        y = tagButton("Connect mail in Cursor", #selector(openCursor(_:)), 0, y)
        toggle("mail", "Also read the mail window that is already open", mailFront, y)
        extraHeight = y + 36
    }

    func calendarPage() {
        var y = linkButton("Connect calendar in Claude", "https://claude.ai/settings/connectors", 0)
        y = linkButton("Connect calendar in ChatGPT", "https://chatgpt.com/settings/connectors", y)
        y = tagButton("Connect calendar in Cursor", #selector(openCursor(_:)), 1, y)
        extraHeight = y + 8
    }

    func notesPage() {
        var y = CGFloat(0)
        let paths = notePaths()
        if paths.isEmpty {
            y = tagButton("Create my notes folder", #selector(makeNotes(_:)), 0, y)
        }
        for (i, path) in paths.enumerated() {
            let name = URL(fileURLWithPath: path).lastPathComponent
            y = tagButton("Use " + (name.isEmpty ? "this folder" : name), #selector(useVault(_:)), i, y)
        }
        if !paths.isEmpty {
            y = tagButton("Start a new notes folder", #selector(makeNotes(_:)), 0, y)
        }
        if !notesPath.isEmpty {
            y = caption("Ready. A click searches " + URL(fileURLWithPath: notesPath).lastPathComponent + ".", y)
        }
        let done = [mailFront ? "mail" : "", calendarOn ? "calendar" : ""].filter { !$0.isEmpty }
        if !done.isEmpty {
            y = caption("From the last steps: " + done.joined(separator: " and ") + ".", y)
        }
        extraHeight = y + 8
    }

    func aiPage() {
        var y = caption("Claude, Codex, or Cursor. A login already on this Mac is used.", 0)
        y = choiceButton("Connect Claude", "claude", y)
        y = choiceButton("Connect Codex", "codex", y)
        y = choiceButton("Connect Cursor", "cursor", y)
        y = choiceButton(showCustom ? "API key" : "I have an API key", "custom", y)
        if showCustom {
            y = caption("Paid works anywhere this Mac is online. OpenAI-style /v1.", y)
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
        y = caption("A local model is uncommon. Most people stop here.", y)
        if !showLocalAssist {
            y = tagButton("Help me use a local model", #selector(openLocalHelp(_:)), 0, y)
            extraHeight = y + 8
            return
        }
        y = localAssist(y)
        extraHeight = y + 8
    }

    func localPage() {
        extraHeight = localAssist(0) + 8
    }

    func localAssist(_ y: CGFloat) -> CGFloat {
        var y = y
        let known = establishedLocal()
        if !known.isEmpty {
            y = caption("Connected: " + known, y)
        }
        y = caption("It has to already be running on this Mac, or on a computer you already ssh to. We look for it. You do not type a name.", y)
        if scanning {
            y = caption("Looking now.", y)
        } else if foundModels.isEmpty {
            y = caption("Nothing answered. Start Ollama or LM Studio on this Mac, then press Look again. The AI login above still works if you skip this.", y)
        }
        for (i, row) in foundModels.enumerated() {
            let name = (row["model"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let whereText = row["where"] ?? "This Mac"
            let title = name.isEmpty ? "Use the model on " + whereText : name + " on " + whereText
            y = tagButton(title, #selector(useFoundModel(_:)), i, y)
        }
        y = tagButton("Look again", #selector(lookAgain(_:)), 0, y)
        if !showManualLocal {
            return tagButton("It is on another computer", #selector(showManual(_:)), 0, y)
        }
        y = caption("Type the computer and the address. The name shows after it answers.", y)
        for (i, row) in locals.enumerated() {
            y = field("host-\(i)", "ssh host name. Blank if it is on this Mac.", row.host, y)
            y = field("remote-\(i)", "http://127.0.0.1:8080/v1", row.remote, y)
            y = field("url-\(i)", "Or on this Mac. http://127.0.0.1:11434/v1", row.url, y)
            y = rowButtons(probe: 1000 + i, remove: 3000 + i, showRemove: locals.count > 1, y)
        }
        if locals.count < 4 {
            y = addButton("Add another address", #selector(addLocal), y)
        }
        return y
    }

    func establishedLocal() -> String {
        for row in locals {
            let name = row.model.trimmingCharacters(in: .whitespacesAndNewlines)
            let host = row.host.trimmingCharacters(in: .whitespacesAndNewlines)
            let url = row.url.trimmingCharacters(in: .whitespacesAndNewlines)
            let remote = row.remote.trimmingCharacters(in: .whitespacesAndNewlines)
            if host.isEmpty && url.isEmpty && remote.isEmpty { continue }
            if name.isEmpty || name == "local" { continue }
            let whereText = host.isEmpty ? "this Mac" : host
            return name + " on " + whereText
        }
        return ""
    }

    func applyScan(_ doc: [String: Any]) {
        mailApps = doc["mail_apps"] as? [String] ?? []
        chatApps = doc["chat_apps"] as? [String] ?? []
        littlebirdOn = (doc["littlebird"] as? Bool) ?? chatApps.contains("Littlebird")
        foundVaults = doc["vaults"] as? [String] ?? []
        if !mailApps.isEmpty { mailFront = true }
        if !chatApps.isEmpty { chatFront = true }
        if notesPath.isEmpty, let vault = foundVaults.first, !vault.isEmpty {
            notesOn = true
            notesPath = vault
            obsidian = vault
        }
        var rows: [[String: String]] = []
        if let found = doc["models"] as? [[String: Any]] {
            for row in found {
                let url = row["url"] as? String ?? ""
                if url.isEmpty { continue }
                rows.append([
                    "where": row["where"] as? String ?? "This Mac",
                    "host": row["host"] as? String ?? "",
                    "url": url,
                    "model": row["model"] as? String ?? "",
                ])
            }
        }
        foundModels = rows
        scanning = false
        scanned = true
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

    @discardableResult
    func linkButton(_ title: String, _ url: String, _ y: CGFloat) -> CGFloat {
        let button = NSButton(title: title, target: self, action: #selector(openLink(_:)))
        button.identifier = NSUserInterfaceItemIdentifier(url)
        button.frame = NSRect(x: 0, y: y, width: 460, height: 36)
        button.bezelStyle = .rounded
        extra.addSubview(button)
        return y + 44
    }

    @discardableResult
    func tagButton(_ title: String, _ action: Selector, _ tag: Int, _ y: CGFloat) -> CGFloat {
        let button = NSButton(title: title, target: self, action: action)
        button.tag = tag
        button.frame = NSRect(x: 0, y: y, width: 460, height: 36)
        button.bezelStyle = .rounded
        extra.addSubview(button)
        return y + 44
    }

    @objc func openLink(_ sender: NSButton) {
        let raw = sender.identifier?.rawValue ?? ""
        guard let url = URL(string: raw) else { return }
        NSWorkspace.shared.open(url)
        statusField.stringValue = "The browser is open. Turn it on there, then press Next."
    }

    @objc func openCursor(_ sender: NSButton) {
        let app = URL(fileURLWithPath: "/Applications/Cursor.app")
        if FileManager.default.fileExists(atPath: app.path) {
            NSWorkspace.shared.open(app)
            statusField.stringValue = "Cursor is open. In Settings, add the account, then press Next."
            return
        }
        if let url = URL(string: "https://cursor.com/dashboard") {
            NSWorkspace.shared.open(url)
            statusField.stringValue = "The browser is open. Sign in, then press Next."
        }
    }

    func notePaths() -> [String] {
        if !foundVaults.isEmpty { return foundVaults }
        return obsidian.isEmpty ? [] : [obsidian]
    }

    @objc func makeNotes(_ sender: NSButton) {
        seedNotes(path: "")
    }

    func seedNotes(path: String) {
        capture()
        var payload: [String: Any] = ["mail": mailFront, "calendar": calendarOn, "app": pickedApp]
        if !path.isEmpty { payload["path"] = path }
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-notes.json")
        do {
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            feedNote = "The notes folder was not created."
            render()
            return
        }
        statusField.stringValue = "Writing the starter. The model key stays off this app. paisty does not keep it."
        DispatchQueue.global(qos: .userInitiated).async {
            let done = SetupBoard.run(["onboard", "--pull", "notes", "--answers", url.path])
            try? FileManager.default.removeItem(at: url)
            let lines = done.out.split(separator: "\n").map { String($0) }
            let folder = lines.first { $0.hasPrefix("path ") }.map { String($0.dropFirst(5)) } ?? ""
            DispatchQueue.main.async {
                if folder.isEmpty || done.code != 0 {
                    self.feedNote = "The notes folder was not created."
                    self.statusField.stringValue = self.feedNote
                    self.render()
                    return
                }
                self.notesOn = true
                self.notesPath = folder
                self.feedNote = "Notes are ready."
                self.statusField.stringValue = "A click searches that folder."
                let app = "/Applications/Obsidian.app"
                if FileManager.default.fileExists(atPath: app) {
                    NSWorkspace.shared.open(
                        [URL(fileURLWithPath: folder)],
                        withApplicationAt: URL(fileURLWithPath: app),
                        configuration: NSWorkspace.OpenConfiguration()
                    )
                }
                self.render()
            }
        }
    }

    @objc func useVault(_ sender: NSButton) {
        let paths = notePaths()
        guard paths.indices.contains(sender.tag) else { return }
        seedNotes(path: paths[sender.tag])
    }

    @objc func useFoundModel(_ sender: NSButton) {
        guard foundModels.indices.contains(sender.tag) else { return }
        let row = foundModels[sender.tag]
        let host = (row["host"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let url = (row["url"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let name = (row["model"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let hop = LocalHop(host: host, remote: host.isEmpty ? "" : url, url: host.isEmpty ? url : "", model: name.isEmpty ? "local" : name)
        if locals.count == 1 && locals[0].host.isEmpty && locals[0].url.isEmpty && locals[0].remote.isEmpty {
            locals = [hop]
        } else if !locals.contains(where: { $0.host == hop.host && $0.url == hop.url && $0.remote == hop.remote }) {
            locals.insert(hop, at: 0)
            if locals.count > 4 { locals = Array(locals.prefix(4)) }
        }
        statusField.stringValue = name.isEmpty ? "That model will be tried first." : name
        render()
    }

    @objc func lookAgain(_ sender: NSButton) {
        scanning = true
        statusField.stringValue = "Looking on this Mac and on your ssh computers."
        render()
        DispatchQueue.global(qos: .userInitiated).async {
            let scanText = SetupBoard.run(["onboard", "--scan"]).out
            DispatchQueue.main.async {
                if let data = scanText.data(using: .utf8),
                   let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.applyScan(doc)
                } else {
                    self.scanning = false
                    self.scanned = true
                }
                self.render()
            }
        }
    }

    @objc func openLocalHelp(_ sender: NSButton) {
        showLocalAssist = true
        if !scanned {
            lookAgain(sender)
            return
        }
        render()
    }

    @objc func showManual(_ sender: NSButton) {
        capture()
        showManualLocal = true
        render()
    }

    func foundPage() {
        var y = CGFloat(0)
        if scanning && !scanned {
            y = caption("Looking at this Mac.", y)
        } else if mailApps.isEmpty && chatApps.isEmpty && foundVaults.isEmpty {
            y = caption("Nothing already here could be turned on. Next connects an AI.", y)
        } else {
            if !mailApps.isEmpty {
                y = caption("Mail is on: " + mailApps.joined(separator: ", ") + ". A click reads the open window.", y)
            }
            let chats = chatApps.filter { $0 != "Littlebird" }
            if !chats.isEmpty {
                y = caption("Chat is on: " + chats.joined(separator: ", ") + ". A click reads the open window.", y)
            }
            if littlebirdOn {
                y = caption("Littlebird is on this Mac. A click reads that window. paisty does not sign in.", y)
            }
            if notesOn, let vault = foundVaults.first, !vault.isEmpty {
                let name = URL(fileURLWithPath: vault).lastPathComponent
                y = caption("Obsidian vault " + name + " is the notes folder.", y)
            }
        }
        if foundVaults.isEmpty {
            y = tagButton("Create my notes folder", #selector(makeNotes(_:)), 0, y)
        }
        extraHeight = y + 8
    }

    func feedPage() {
        var y = tagButton("Add Apple Notes", #selector(pullSource(_:)), 1, 0)
        y = tagButton("Add OneNote", #selector(pullSource(_:)), 2, y)
        y = tagButton("Add Monday", #selector(pullSource(_:)), 3, y)
        y = field("monday", "Monday token. Stays on this Mac.", "", y, secure: true)
        y = tagButton("Add Littlebird", #selector(pullSource(_:)), 4, y)
        y = tagButton("Add Zoom call notes", #selector(pullSource(_:)), 5, y)
        y = tagButton("Add a notes file", #selector(pickFeed(_:)), 6, y)
        y = tagButton("Add a mail export", #selector(pickFeed(_:)), 7, y)
        y = tagButton("Add a calendar file", #selector(pickFeed(_:)), 8, y)
        if !feedNote.isEmpty {
            y = caption(feedNote, y)
        }
        extraHeight = y + 8
    }

    @objc func pickFeed(_ sender: NSButton) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.prompt = "Add"
        panel.message = "The next click can search what you add. The password is not read."
        panel.beginSheetModal(for: window) { response in
            guard response == .OK else { return }
            self.ingest(panel.urls.map { $0.path })
        }
    }

    func ingest(_ paths: [String]) {
        guard !paths.isEmpty else { return }
        let payload: [String: Any] = ["paths": paths]
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-ingest.json")
        do {
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            feedNote = "Those files were not added."
            render()
            return
        }
        statusField.stringValue = "Adding those files."
        DispatchQueue.global(qos: .userInitiated).async {
            let done = SetupBoard.run(["onboard", "--ingest", "--answers", url.path])
            try? FileManager.default.removeItem(at: url)
            let line = done.out.trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                if line.hasPrefix("added 0") || done.code != 0 {
                    self.feedNote = "Nothing in that choice could be added. Use notes, mail, calendar, or a text file."
                } else {
                    self.feedNote = "Added. The next click can search them."
                }
                self.statusField.stringValue = self.feedNote
                self.render()
            }
        }
    }

    @objc func pullSource(_ sender: NSButton) {
        capture()
        let names = [1: "apple-notes", 2: "onenote", 3: "monday", 4: "littlebird", 5: "zoom"]
        guard let name = names[sender.tag] else { return }
        let apps = [
            "onenote": "/Applications/Microsoft OneNote.app",
            "littlebird": "/Applications/Littlebird.app",
        ]
        if let path = apps[name], FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        }
        var payload: [String: Any] = [:]
        if name == "monday" {
            let token = fields["monday"]?.stringValue ?? ""
            if !token.isEmpty { payload["token"] = token }
        }
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-pull.json")
        do {
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            feedNote = "That source was not added."
            render()
            return
        }
        statusField.stringValue = "Adding that source."
        DispatchQueue.global(qos: .userInitiated).async {
            let done = SetupBoard.run(["onboard", "--pull", name, "--answers", url.path])
            try? FileManager.default.removeItem(at: url)
            let line = done.out.trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                if line.contains("need-token") {
                    self.feedNote = "Paste the Monday token in the field. It stays on this Mac."
                } else if line.hasPrefix("added 0") || done.code != 0 {
                    self.feedNote = "No text notes were there yet. Export from that app, then press the button again."
                } else {
                    self.feedNote = "Added. The next click can search them."
                }
                self.statusField.stringValue = self.feedNote
                self.render()
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
            let localIndex = tag >= 2000 ? -1 : tag - 1000
            DispatchQueue.main.async {
                if localIndex >= 0, text.hasPrefix("Connected.") {
                    let rest = text.dropFirst("Connected.".count).trimmingCharacters(in: .whitespacesAndNewlines)
                    if !rest.isEmpty, !rest.hasPrefix("The key"), self.locals.indices.contains(localIndex) {
                        self.locals[localIndex].model = String(rest.prefix(64))
                    }
                }
                self.statusField.stringValue = text.isEmpty ? "Not reachable." : String(text.prefix(200))
                if localIndex >= 0 { self.render() }
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
