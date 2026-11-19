import Cocoa
import CryptoKit

/// Self-update. Every build on main publishes a signed feed; installed copies check it and
/// replace themselves in place. The feed and the zip are verified with an Ed25519 key that is
/// compiled into the app, and the zip's SHA-256 must match the signed feed. Nothing about the
/// user is sent: the request is a plain GET of a public file.
final class PaistyUpdater {
    static let shared = PaistyUpdater()
    static let autoKey = "paistyAutoUpdate"
    static let defaultFeed = "https://github.com/hailports/StackMini/releases/download/paisty-mac/paisty-update.json"
    static let urlPrefix = "https://github.com/hailports/StackMini/releases/download/"

    var timer: Timer?
    var checking = false
    /// The app tells the updater when a draft is running so an install never interrupts one.
    var isBusy: () -> Bool = { false }

    var currentBuild: Int {
        Int(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0") ?? 0
    }

    var currentVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    }

    var feedURL: URL? {
        if let set = ProcessInfo.processInfo.environment["PAISTY_UPDATE_FEED"], !set.isEmpty { return URL(string: set) }
        let raw = Bundle.main.object(forInfoDictionaryKey: "PaistyFeedURL") as? String ?? Self.defaultFeed
        return URL(string: raw)
    }

    var publicKey: Curve25519.Signing.PublicKey? {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "PaistyEdPublicKey") as? String,
              let data = Data(base64Encoded: raw) else { return nil }
        return try? Curve25519.Signing.PublicKey(rawRepresentation: data)
    }

    var autoOn: Bool {
        UserDefaults.standard.register(defaults: [Self.autoKey: true])
        return UserDefaults.standard.bool(forKey: Self.autoKey)
    }

    func start() {
        let delay = Double(ProcessInfo.processInfo.environment["PAISTY_UPDATE_DELAY"] ?? "") ?? 45
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.check(userAsked: false) }
        timer = Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { [weak self] _ in
            self?.check(userAsked: false)
        }
    }

    func log(_ line: String) {
        let dir = NSHomeDirectory() + "/.config/paiste"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let path = dir + "/update.log"
        let stamp = ISO8601DateFormatter().string(from: Date())
        let row = "\(stamp) \(line)\n"
        if let handle = FileHandle(forWritingAtPath: path) {
            handle.seekToEndOfFile()
            handle.write(row.data(using: .utf8)!)
            handle.closeFile()
        } else {
            try? row.write(toFile: path, atomically: true, encoding: .utf8)
        }
    }

    /// The exact bytes the release signs.
    static func signedMessage(build: Int, version: String, sha256: String, url: String) -> Data {
        "paisty-update\n\(build)\n\(version)\n\(sha256.lowercased())\n\(url)".data(using: .utf8)!
    }

    struct Offer {
        let build: Int
        let version: String
        let url: URL
        let sha256: String
        let notes: String
    }

    /// Parse and verify a feed. Returns nil unless the signature checks out.
    static func verifiedOffer(_ data: Data, key: Curve25519.Signing.PublicKey) -> Offer? {
        guard let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let build = (doc["build"] as? NSNumber)?.intValue,
              let version = doc["version"] as? String,
              let urlString = doc["url"] as? String,
              let sha = doc["sha256"] as? String,
              let sigB64 = doc["sig"] as? String,
              let sig = Data(base64Encoded: sigB64),
              urlString.hasPrefix(urlPrefix),
              let url = URL(string: urlString) else { return nil }
        let message = signedMessage(build: build, version: version, sha256: sha, url: urlString)
        guard key.isValidSignature(sig, for: message) else { return nil }
        return Offer(build: build, version: version, url: url, sha256: sha.lowercased(), notes: doc["notes"] as? String ?? "")
    }

    func check(userAsked: Bool) {
        guard !checking else { return }
        guard let feed = feedURL, let key = publicKey else {
            log("no feed or key in this build")
            if userAsked { notify("Updates are not set up in this build") }
            return
        }
        checking = true
        var req = URLRequest(url: feed, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        req.setValue("paisty/\(currentVersion)", forHTTPHeaderField: "User-Agent")
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
            guard let self else { return }
            defer { self.checking = false }
            guard let data, err == nil, (resp as? HTTPURLResponse)?.statusCode == 200 else {
                self.log("feed fetch failed")
                if userAsked { self.notify("Could not reach the update feed") }
                return
            }
            guard let offer = Self.verifiedOffer(data, key: key) else {
                self.log("feed signature or format rejected")
                if userAsked { self.notify("Update feed failed verification") }
                return
            }
            guard offer.build > self.currentBuild else {
                self.log("up to date at \(self.currentBuild) (feed \(offer.build))")
                if userAsked { self.notify("paisty \(self.currentVersion) is up to date") }
                return
            }
            self.log("update \(self.currentBuild) -> \(offer.build)")
            if self.autoOn || userAsked {
                self.download(offer)
            }
        }.resume()
    }

    func download(_ offer: Offer) {
        URLSession.shared.downloadTask(with: offer.url) { [weak self] tmp, resp, err in
            guard let self else { return }
            guard let tmp, err == nil, (resp as? HTTPURLResponse)?.statusCode == 200,
                  let data = try? Data(contentsOf: tmp, options: .mappedIfSafe) else {
                self.log("download failed")
                return
            }
            let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
            guard digest == offer.sha256 else {
                self.log("sha256 mismatch; update discarded")
                return
            }
            let work = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-update-\(offer.build)")
            try? FileManager.default.removeItem(at: work)
            try? FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
            let zip = work.appendingPathComponent("paisty.zip")
            do { try data.write(to: zip) } catch { self.log("write failed"); return }
            guard Self.run("/usr/bin/ditto", ["-x", "-k", zip.path, work.path]) == 0 else {
                self.log("unzip failed")
                return
            }
            let app = work.appendingPathComponent("paisty.app")
            guard let info = NSDictionary(contentsOf: app.appendingPathComponent("Contents/Info.plist")),
                  info["CFBundleIdentifier"] as? String == Bundle.main.bundleIdentifier,
                  Int(info["CFBundleVersion"] as? String ?? "0") == offer.build else {
                self.log("bundle check failed")
                return
            }
            guard Self.run("/usr/bin/codesign", ["--verify", "--deep", app.path]) == 0 else {
                self.log("codesign verify failed")
                return
            }
            self.waitIdleThenInstall(app, offer: offer)
        }.resume()
    }

    func waitIdleThenInstall(_ app: URL, offer: Offer) {
        DispatchQueue.main.async {
            if self.isBusy() {
                DispatchQueue.main.asyncAfter(deadline: .now() + 20) { self.waitIdleThenInstall(app, offer: offer) }
                return
            }
            self.install(app, offer: offer)
        }
    }

    func install(_ app: URL, offer: Offer) {
        let current = Bundle.main.bundleURL
        let parent = current.deletingLastPathComponent()
        guard FileManager.default.isWritableFile(atPath: parent.path) else {
            log("cannot write \(parent.path); update left at \(app.path)")
            notify("paisty \(offer.version) is ready. Move it from Downloads to Applications to finish.")
            let dest = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads/paisty.app")
            try? FileManager.default.removeItem(at: dest)
            try? FileManager.default.copyItem(at: app, to: dest)
            return
        }
        let pid = ProcessInfo.processInfo.processIdentifier
        let backup = parent.appendingPathComponent(".paisty-previous.app")
        let script = """
        #!/bin/sh
        while kill -0 \(pid) 2>/dev/null; do sleep 0.3; done
        rm -rf '\(backup.path)'
        mv '\(current.path)' '\(backup.path)' || exit 1
        if mv '\(app.path)' '\(current.path)'; then
          xattr -dr com.apple.quarantine '\(current.path)' 2>/dev/null
          rm -rf '\(backup.path)'
          open '\(current.path)'
        else
          mv '\(backup.path)' '\(current.path)'
          open '\(current.path)'
        fi
        """
        let path = FileManager.default.temporaryDirectory.appendingPathComponent("paisty-swap.sh")
        do {
            try script.write(to: path, atomically: true, encoding: .utf8)
        } catch {
            log("swap script write failed")
            return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        task.arguments = [path.path]
        do { try task.run() } catch { log("swap launch failed"); return }
        log("installing \(offer.version) (\(offer.build)); relaunching")
        NSApp.terminate(nil)
    }

    func notify(_ text: String) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.messageText = "paisty"
            alert.informativeText = text
            alert.addButton(withTitle: "OK")
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }

    @discardableResult
    static func run(_ tool: String, _ args: [String]) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: tool)
        task.arguments = args
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return -1 }
        task.waitUntilExit()
        return task.terminationStatus
    }
}
