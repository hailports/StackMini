import Foundation

/// Reply hint text logic. Pure: no AX, no network, no capture, so the harness can test it.
/// Glow only when the newest message on screen is from someone else and asks something.
/// A window that only shows my own note, a read-only page, or browser tab titles does not glow.
enum ReplyHint {
    static let chromeRoles: Set<String> = [
        "AXOpaqueProviderGroup", "AXTab", "AXTabGroup", "AXToolbar", "AXMenuBar", "AXMenuItem",
        "AXButton", "AXRadioButton", "AXPopUpButton", "AXCheckBox",
    ]
    static let askWords = [
        "can you", "could you", "would you", "will you", "can we", "could we", "please", "pls",
        "let me know", "lmk", "thoughts", "any update", "need you", "need your", "are you", "do you",
        "did you", "have you", "is this", "should i", "should we", "when can", "what do you",
        "your call", "ok with", "okay with", "any chance",
    ]

    struct Turn {
        var author: String
        var body: String
    }

    /// The names that count as me. From route.yaml `me:` and the account's full name.
    static func meNames(routeText: String, fullName: String) -> [String] {
        var out: [String] = []
        for raw in routeText.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard line.hasPrefix("me:") else { continue }
            let value = line.dropFirst(3).trimmingCharacters(in: CharacterSet(charactersIn: " \"'"))
            out += value.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        }
        out.append(fullName)
        return out.map { $0.lowercased() }.filter { $0.count >= 2 }
    }

    static func isMe(_ author: String, me: [String]) -> Bool {
        let low = author.lowercased()
        guard !low.isEmpty else { return false }
        let words = Set(low.split { !$0.isLetter }.map(String.init))
        for name in me {
            if name.contains(" ") {
                if low.contains(name) { return true }
            } else if words.contains(name) {
                return true
            }
        }
        return low == "me" || low == "you" || low.hasPrefix("me ") || low.hasPrefix("you ")
    }

    static func chrome(_ role: String, _ text: String, title: String) -> Bool {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if body.isEmpty || chromeRoles.contains(role) { return true }
        let low = body.lowercased()
        if low.hasPrefix("tab bar") { return true }
        if !title.isEmpty && body == title.trimmingCharacters(in: .whitespacesAndNewlines) { return true }
        if body.count <= 160 && (low.contains("ctrl+") || low.contains("cmd+") || low.contains("shortcut") || low.contains("type / for more")) {
            return true
        }
        return false
    }

    /// A header line that starts a new message, and who wrote it.
    static func header(_ line: String) -> (author: String, mail: Bool)? {
        let text = line.trimmingCharacters(in: .whitespaces)
        if text.lowercased().hasPrefix("from:") {
            return (String(text.dropFirst(5)).trimmingCharacters(in: .whitespaces), true)
        }
        if let r = text.range(of: #"^(.{2,60}?)\s+(commented|replied|wrote)\b"#, options: [.regularExpression, .caseInsensitive]) {
            let who = String(text[r]).replacingOccurrences(of: #"\s+(commented|replied|wrote)\b.*$"#, with: "", options: [.regularExpression, .caseInsensitive])
            return (who, false)
        }
        if text.count <= 80, let r = text.range(of: #"^(.{2,60}?)\s+\d{1,2}:\d{2}\s?(AM|PM|am|pm)?\s*$"#, options: .regularExpression) {
            let who = String(text[r]).replacingOccurrences(of: #"\s+\d{1,2}:\d{2}.*$"#, with: "", options: .regularExpression)
            return (who, false)
        }
        return nil
    }

    /// Who said what, in on-screen order. A "commented:" header may carry its body on the same line.
    static func turns(_ lines: [String]) -> (turns: [Turn], mail: Bool) {
        var out: [Turn] = []
        var mail = false
        var current = Turn(author: "", body: "")
        for line in lines {
            let low = line.trimmingCharacters(in: .whitespaces).lowercased()
            let meta = ["sent:", "to:", "cc:", "bcc:", "subject:", "date:"].contains { low.hasPrefix($0) }
            if meta {
                if current.author.isEmpty { current.body += line + "\n" }
                continue
            }
            if let head = header(line) {
                if !current.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !current.author.isEmpty { out.append(current) }
                mail = mail || head.mail
                var rest = ""
                if !head.mail, let colon = line.range(of: #"\b(commented|replied|wrote)\b[^:]{0,40}:"#, options: [.regularExpression, .caseInsensitive]) {
                    rest = String(line[colon.upperBound...])
                }
                current = Turn(author: head.author, body: rest)
                continue
            }
            current.body += line + "\n"
        }
        if !current.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !current.author.isEmpty { out.append(current) }
        return (out, mail)
    }

    static func asks(_ body: String, me: [String]) -> Bool {
        let low = " " + body.lowercased() + " "
        if low.contains("?") { return true }
        if askWords.contains(where: { low.contains($0) }) { return true }
        for name in me where !name.contains(" ") {
            if low.range(of: "[^a-z]" + NSRegularExpression.escapedPattern(for: name) + "[,:\\-]", options: .regularExpression) != nil { return true }
            if low.contains("@" + name) { return true }
        }
        return false
    }

    /// fieldText is the box I am typing in. Mail reply boxes carry the quoted thread under my text.
    static func waiting(fieldText: String, blocks: [[String: Any]], title: String, me: [String], newestFirst: Bool = false) -> Bool {
        var lines: [String] = []
        let mine = fieldText.trimmingCharacters(in: .whitespacesAndNewlines)
        for row in blocks {
            let text = (row["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let role = row["role"] as? String ?? ""
            if text == mine || chrome(role, text, title: title) { continue }
            lines += text.components(separatedBy: "\n")
        }
        if mine.lowercased().contains("from:") {
            lines += fieldText.components(separatedBy: "\n")
        }
        let read = turns(lines)
        let authored = read.turns.filter { !$0.author.isEmpty }
        if authored.isEmpty {
            // No author on screen. Only a clear ask from the page text counts, and not a short label.
            let all = read.turns.map { $0.body }.joined(separator: "\n")
            return all.count >= 40 && asks(all, me: me)
        }
        // Mail lists the newest message first. Chat and most comment threads list it last.
        guard let newest = (read.mail || newestFirst) ? authored.first : authored.last else { return false }
        if isMe(newest.author, me: me) { return false }
        return asks(newest.body, me: me)
    }

    /// Azure DevOps shows the newest comment first.
    static func newestFirst(title: String, bundle: String) -> Bool {
        let low = title.lowercased()
        return low.hasSuffix(" - boards") || low.contains("azure devops") || low.contains("dev.az" + "ure.com") || low.contains("visualstudio.com")
    }
}
