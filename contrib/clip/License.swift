import Foundation
import CryptoKit
import IOKit

/// Keys turn themselves on. Nobody pastes anything:
/// - first launch asks /api/claim, which hands over the key the email form (or checkout) just issued
///   to this network;
/// - the link in the key email opens paisty://activate?k=…;
/// - pasting still works as a last resort.
/// The key lives in ~/.config/paiste/license (0600). Device keys are bound to this Mac by `dev`,
/// a salted hash of the hardware UUID; the hardware id itself never leaves the Mac.
enum PaistyLicense {
    static let claimURL = URL(string: "https://www.paisty.com/api/claim")!
    static let scheme = "paisty"

    static var path: URL {
        URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".config/paiste/license")
    }

    static func read() -> String {
        ((try? String(contentsOf: path, encoding: .utf8)) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Shape only. The server (or the local signer) decides if a key is good.
    static func plausible(_ key: String) -> Bool {
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard k.count > 40, k.count < 4000, k.range(of: #"^[A-Za-z0-9._-]+$"#, options: .regularExpression) != nil else { return false }
        if k.hasPrefix("p2.") { return k.split(separator: ".").count == 3 }
        return k.split(separator: ".").count == 2
    }

    @discardableResult
    static func save(_ key: String) -> Bool {
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard plausible(k) else { return false }
        let dir = path.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let old = read()
            if !old.isEmpty && old != k {
                let prev = dir.appendingPathComponent("license.prev")
                try? old.write(to: prev, atomically: true, encoding: .utf8)
                try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: prev.path)
            }
            try k.write(to: path, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
            return true
        } catch {
            return false
        }
    }

    /// The key from paisty://activate?k=… or https://www.paisty.com/activate#k=…
    static func activationKey(from url: URL) -> String? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        var key: String?
        if parts.scheme == scheme, parts.host == "activate" || parts.path == "activate" || parts.path == "/activate" {
            key = parts.queryItems?.first(where: { $0.name == "k" })?.value
        } else if parts.scheme == "https", (parts.host ?? "").hasSuffix("paisty.com"), parts.path == "/activate",
                  let frag = parts.fragment {
            key = URLComponents(string: "x:?" + frag)?.queryItems?.first(where: { $0.name == "k" })?.value
        }
        guard let key, plausible(key) else { return nil }
        return key
    }

    /// Salted SHA-256 of the hardware UUID, 32 hex. Stable across reinstalls, unique per Mac.
    static func deviceHash(uuid: String) -> String {
        let digest = SHA256.hash(data: Data(("paisty-device:" + uuid.uppercased()).utf8))
        return digest.map { String(format: "%02x", $0) }.joined().prefix(32).lowercased()
    }

    static let deviceID: String = {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
        defer { IOObjectRelease(service) }
        guard service != 0,
              let raw = IORegistryEntryCreateCFProperty(service, "IOPlatformUUID" as CFString, kCFAllocatorDefault, 0)?
                .takeRetainedValue() as? String, !raw.isEmpty else { return "" }
        return deviceHash(uuid: raw)
    }()

    /// Claims of a hex key (hex(json).sig). p2 keys are sealed; only the server reads them.
    static func claims(_ key: String) -> [String: Any]? {
        guard !key.hasPrefix("p2."), let dot = key.firstIndex(of: ".") else { return nil }
        guard let data = hexData(String(key[..<dot])) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    static func hexData(_ hex: String) -> Data? {
        let s = hex.lowercased()
        guard !s.isEmpty, s.count % 2 == 0, s.allSatisfy({ $0.isHexDigit }) else { return nil }
        var data = Data(capacity: s.count / 2)
        var i = s.startIndex
        while i < s.endIndex {
            let j = s.index(i, offsetBy: 2)
            guard let byte = UInt8(s[i..<j], radix: 16) else { return nil }
            data.append(byte)
            i = j
        }
        return data
    }

    /// The app-side view of a key, used to decide whether to show the key screen.
    /// p2 keys are sealed, so their term is the server's call (subscriptions renew there).
    /// Hex keys carry exp; exp 0 is lifetime. A device key from another Mac is not valid here.
    static func valid(_ key: String, device: String, now: Date = Date()) -> Bool {
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard plausible(k) else { return false }
        if k.hasPrefix("p2.") { return true }
        if boundElsewhere(k, device: device) { return false }
        guard let json = claims(k), let exp = (json["exp"] as? NSNumber)?.doubleValue else { return false }
        if exp == 0 || exp > now.timeIntervalSince1970 { return true }
        // A lapsed subscription key renews on the server; let the draft gate ask before nagging.
        return (json["sub"] as? String ?? "").hasPrefix("cus_")
    }

    static func validHere() -> Bool { valid(read(), device: deviceID) }

    /// A device key copied to another Mac is not valid here.
    static func boundElsewhere(_ key: String, device: String = deviceID) -> Bool {
        guard let dev = claims(key)?["dev"] as? String, !dev.isEmpty else { return false }
        return dev != device
    }

    /// Headers that let a licensed copy reach the update feed even if the baked token rotates.
    static func feedHeaders(key: String = read(), device: String = deviceID) -> [String: String] {
        guard plausible(key) else { return [:] }
        var out = ["X-Paisty-License": key]
        if !device.isEmpty { out["X-Paisty-Device"] = device }
        return out
    }

    /// When to keep asking /api/claim: only while there is no key, every `every` seconds, for `window`.
    static func claimSchedule(hasKey: Bool, every: TimeInterval = 20, window: TimeInterval = 6 * 3600) -> [TimeInterval] {
        guard !hasKey else { return [] }
        var out: [TimeInterval] = [2]
        var t: TimeInterval = 2
        while t + every <= window && out.count < 60 { t += every; out.append(t) }
        // After the first 20 minutes, back off to every 10 minutes for the rest of the window.
        while t + 600 <= window { t += 600; out.append(t) }
        return out
    }

    /// One claim request. Calls back on the main queue with the saved key, or nil.
    static func claim(_ done: @escaping (String?) -> Void) {
        var req = URLRequest(url: claimURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["device": deviceID])
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            var saved: String?
            if (resp as? HTTPURLResponse)?.statusCode == 200,
               let data, let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               doc["ok"] as? Bool == true, let key = doc["token"] as? String, !validHere() {
                if save(key) { saved = key }
            }
            DispatchQueue.main.async { done(saved) }
        }.resume()
    }

    static var claimTimers: [Timer] = []

    /// Keep asking until a key lands or the window closes. Safe to call more than once.
    static func claimUntilActive(onActive: @escaping () -> Void) {
        claimTimers.forEach { $0.invalidate() }
        claimTimers = []
        let start = Date()
        for at in claimSchedule(hasKey: validHere()) {
            let timer = Timer(fire: start.addingTimeInterval(at), interval: 0, repeats: false) { _ in
                guard !validHere() else {
                    claimTimers.forEach { $0.invalidate() }
                    return
                }
                claim { key in
                    guard key != nil else { return }
                    claimTimers.forEach { $0.invalidate() }
                    claimTimers = []
                    onActive()
                }
            }
            RunLoop.main.add(timer, forMode: .common)
            claimTimers.append(timer)
        }
    }
}

/// When the updater looks and how it relaunches. Lives here so the tests can build it without AppKit.
enum UpdateSchedule {
    static let firstCheck: TimeInterval = 20
    static let every: TimeInterval = 3 * 3600

    static func due(lastCheck: Date?, now: Date = Date()) -> Bool {
        guard let lastCheck else { return true }
        return now.timeIntervalSince(lastCheck) >= every
    }

    /// launchd jobs get XPC_SERVICE_NAME = their label; apps opened by Finder or `open` get "application.…" or "0".
    static func underLaunchd(_ env: [String: String]) -> Bool {
        let name = env["XPC_SERVICE_NAME"] ?? ""
        return !name.isEmpty && name != "0" && !name.hasPrefix("application.")
    }
}
