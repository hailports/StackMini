import Cocoa
import CryptoKit

/// Self-update. Every build on main publishes a signed feed; installed copies check it and
/// replace themselves in place. The feed and the zip are verified with an Ed25519 key that is
/// compiled into the app, and the zip's SHA-256 must match the signed feed. Nothing about the
/// user is sent beyond the app's own key: the request is a GET with the baked feed token and the
/// license key, either of which opens the feed.
///
/// When: 20 s after launch, every 3 hours, and on wake when the last check is older than that.
/// Install: swap the bundle in place, then relaunch: launchd's KeepAlive restarts a launchd copy,
/// anything else is reopened by a detached shell.
final class PaistyUpdater {
    static let shared = PaistyUpdater()
    static let autoKey = "paistyAutoUpdate"
    static let defaultFeed = "https://www.paisty.com/api/update/paisty-update.json"
    static let urlPrefix = "https://www.paisty.com/api/update/"
    static let tokenHeader = "X-Paisty-Update"

    var timer: Timer?
    var checking = false
    var lastCheck: Date?
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

    /// Baked in at build time. The update endpoint serves only to copies that carry it.
    var feedToken: String {
        Bundle.main.object(forInfoDictionaryKey: "PaistyFeedToken") as? String ?? ""
    }

    func request(_ url: URL, timeout: TimeInterval) -> URLRequest {
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.setValue("paisty/\(currentVersion)", forHTTPHeaderField: "User-Agent")
        if !feedToken.isEmpty { req.setValue(feedToken, forHTTPHeaderField: Self.tokenHeader) }
        for (name, value) in PaistyLicense.feedHeaders() { req.setValue(value, forHTTPHeaderField: name) }
        return req
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
        let delay = Double(ProcessInfo.processInfo.environment["PAISTY_UPDATE_DELAY"] ?? "") ?? UpdateSchedule.firstCheck
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.check(userAsked: false) }
        timer = Timer.scheduledTimer(withTimeInterval: UpdateSchedule.every, repeats: true) { [weak self] _ in
            self?.check(userAsked: false)
        }
        // A Mac that slept through the timer checks when it wakes.
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self, UpdateSchedule.due(lastCheck: self.lastCheck) else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) { self.check(userAsked: false) }
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
        lastCheck = Date()
        let req = request(feed, timeout: 20)
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
        URLSession.shared.downloadTask(with: request(offer.url, timeout: 300)) { [weak self] tmp, resp, err in
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
        // Swap in place while running (the old binary stays mapped), so nothing depends on a helper
        // outliving this process: launchd kills a job's leftover children when it exits.
        let fm = FileManager.default
        let backup = parent.appendingPathComponent(".paisty-previous.app")
        try? fm.removeItem(at: backup)
        do {
            try fm.moveItem(at: current, to: backup)
        } catch {
            log("swap failed: could not move the running copy aside")
            return
        }
        do {
            try fm.moveItem(at: app, to: current)
        } catch {
            try? fm.moveItem(at: backup, to: current)
            log("swap failed: restored \(currentVersion)")
            return
        }
        Self.run("/usr/bin/xattr", ["-dr", "com.apple.quarantine", current.path])
        try? fm.removeItem(at: backup)
        let env = ProcessInfo.processInfo.environment
        if env["PAISTY_UPDATE_NO_RELAUNCH"] == nil && !UpdateSchedule.underLaunchd(env) {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/bin/sh")
            task.arguments = ["-c", "sleep 1; /usr/bin/open \"$0\"", current.path]
            try? task.run()
        }
        log("installed \(offer.version) (\(offer.build)); relaunching")
        NSApp.terminate(nil)
    }

    /// `paisty --check-update`. Asks the feed as the app does (baked token + key), and once with the key
    /// alone, verifies the signature, and prints one line each. Exit 0 when the feed answered and verified.
    func dryRun() -> Int32 {
        guard let feed = feedURL, let key = publicKey else { print("no feed or key in this build"); return 2 }
        func fetch(_ req: URLRequest) -> (Int, Data?) {
            let sem = DispatchSemaphore(value: 0)
            var out: (Int, Data?) = (0, nil)
            URLSession.shared.dataTask(with: req) { data, resp, _ in
                out = ((resp as? HTTPURLResponse)?.statusCode ?? 0, data)
                sem.signal()
            }.resume()
            _ = sem.wait(timeout: .now() + 25)
            return out
        }
        let both = request(feed, timeout: 20)
        var keyOnly = both
        keyOnly.setValue(nil, forHTTPHeaderField: Self.tokenHeader)
        let (status, data) = fetch(both)
        let (keyStatus, _) = PaistyLicense.feedHeaders().isEmpty ? (0, nil) : fetch(keyOnly)
        let device = PaistyLicense.deviceID.isEmpty ? "none" : String(PaistyLicense.deviceID.prefix(6)) + "…"
        print("installed \(currentVersion) (\(currentBuild)) device \(device) license \(SetupBoard.licenseState())")
        print("feed http \(status); with the license key alone http \(keyStatus)")
        guard status == 200, let data, let offer = Self.verifiedOffer(data, key: key) else {
            print("feed not verified")
            return 1
        }
        let next = offer.build > currentBuild ? "would install \(offer.version) (\(offer.build))" : "up to date"
        print("feed \(offer.version) (\(offer.build)) signature ok; \(next); auto \(autoOn ? "on" : "off"); every \(Int(UpdateSchedule.every / 3600)) h")
        return 0
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
