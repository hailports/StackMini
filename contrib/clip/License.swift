import Foundation
import CryptoKit
import IOKit

/// Keys turn themselves on. Nobody pastes anything:
/// - first launch asks /api/claim, which hands over the key a paid checkout just issued to this network,
///   or, after a trial signup, a trial entitlement (see PaistyEntitlement; it is not a key);
/// - the link in the key email opens paisty://activate?k=…;
/// - pasting still works as a last resort for paid keys. Nothing is pasted during a trial.
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

    /// What a claim answer means. A key is saved as a license. A trial entitlement is kept apart and
    /// never written to the license file. "ended" means the trial's subscription is gone; "past_due" and
    /// "unpaid" mean the trial is over but a late payment can still bring the key.
    enum ClaimResult: Equatable { case nothing, license, trial, over, ended }

    static func readClaim(_ doc: [String: Any]) -> (result: ClaimResult, key: String?, entitlement: String?, ends: Double) {
        if doc["ok"] as? Bool == true, let key = doc["token"] as? String, plausible(key) { return (.license, key, nil, 0) }
        if doc["ok"] as? Bool == true, let ent = doc["entitlement"] as? String, PaistyEntitlement.plausible(ent) {
            return (.trial, nil, ent, (doc["ends"] as? NSNumber)?.doubleValue ?? 0)
        }
        switch doc["trial"] as? String ?? "" {
        case "ended": return (.ended, nil, nil, 0)
        case "past_due", "unpaid": return (.over, nil, nil, 0)
        default: return (.nothing, nil, nil, 0)
        }
    }

    /// How often a running trial asks for its paid key, and for how long after the trial end.
    static let trialEvery: TimeInterval = 1800
    static let trialTail: TimeInterval = 14 * 24 * 3600

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

    /// One claim request. With a trial entitlement it asks for the paid key; without one it asks for what
    /// this network was just given. Calls back on the main queue. A valid key is never replaced.
    static func claim(_ done: @escaping (ClaimResult) -> Void) {
        var req = URLRequest(url: claimURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["device": deviceID]
        if let ent = PaistyEntitlement.read() { body["entitlement"] = ent.token }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            var result = ClaimResult.nothing
            if (resp as? HTTPURLResponse)?.statusCode == 200,
               let data, let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let read = readClaim(doc)
                switch read.result {
                case .license:
                    if !validHere(), let key = read.key, save(key) {
                        PaistyEntitlement.clear()
                        result = .license
                    }
                case .trial:
                    if !validHere(), let ent = read.entitlement, PaistyEntitlement.save(ent, ends: read.ends) { result = .trial }
                case .over:
                    PaistyEntitlement.markOver()
                    result = .over
                case .ended:
                    PaistyEntitlement.clear(ended: true)
                    result = .ended
                case .nothing:
                    break
                }
            }
            DispatchQueue.main.async { done(result) }
        }.resume()
    }

    static var claimTimers: [Timer] = []

    /// One trial poll: ask for the paid key. Stops for good once it lands, the subscription is gone,
    /// or the poll window after the trial end has passed.
    static func trialTick(stopAt: Date, onActive: @escaping () -> Void) {
        guard !validHere(), Date() < stopAt, PaistyEntitlement.read() != nil else {
            claimTimers.forEach { $0.invalidate() }
            claimTimers = []
            return
        }
        claim { result in
            guard result == .license || result == .ended else { return }
            claimTimers.forEach { $0.invalidate() }
            claimTimers = []
            if result == .license { onActive() }
        }
    }

    /// Keep asking until a key lands or the window closes. Safe to call more than once.
    /// No key and no trial: soon after launch, for 6 hours. A trial: every 30 minutes for its paid key,
    /// until it lands or 14 days after the trial end.
    static func claimUntilActive(onActive: @escaping () -> Void) {
        claimTimers.forEach { $0.invalidate() }
        claimTimers = []
        guard !validHere() else { return }
        if let ent = PaistyEntitlement.read() {
            let stopAt = ent.ends.timeIntervalSince1970 == 0 ? Date().addingTimeInterval(21 * 24 * 3600) : ent.ends.addingTimeInterval(trialTail)
            let first = Timer(fire: Date().addingTimeInterval(2), interval: 0, repeats: false) { _ in
                trialTick(stopAt: stopAt, onActive: onActive)
            }
            let every = Timer(fire: Date().addingTimeInterval(trialEvery), interval: trialEvery, repeats: true) { _ in
                trialTick(stopAt: stopAt, onActive: onActive)
            }
            RunLoop.main.add(first, forMode: .common)
            RunLoop.main.add(every, forMode: .common)
            claimTimers = [first, every]
            return
        }
        let start = Date()
        for at in claimSchedule(hasKey: false) {
            let timer = Timer(fire: start.addingTimeInterval(at), interval: 0, repeats: false) { _ in
                guard !validHere(), PaistyEntitlement.read() == nil else {
                    claimTimers.forEach { $0.invalidate() }
                    return
                }
                claim { result in
                    guard result == .license || result == .trial else { return }
                    claimTimers.forEach { $0.invalidate() }
                    claimTimers = []
                    onActive()
                    // A trial just landed: switch to asking for its paid key.
                    if result == .trial { claimUntilActive(onActive: onActive) }
                }
            }
            RunLoop.main.add(timer, forMode: .common)
            claimTimers.append(timer)
        }
    }
}

/// A trial has no license. A trial signup gives the app a signed entitlement instead: the thanks page
/// parks it for this network and /api/claim hands it over, or paisty://trial?e=… from the thanks page.
/// It lives in ~/.config/paiste/entitlement (0600), never in the license file, and is never shown or pasted.
/// The draft gate sends it to /api/license. The first paid invoice turns into a normal license, which the
/// claim poll saves silently; then this file is deleted.
enum PaistyEntitlement {
    struct Stored: Equatable {
        let token: String
        let ends: Date
        let over: Bool
    }

    static var path: URL {
        URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".config/paiste/entitlement")
    }

    static var endedMark: URL {
        URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".config/paiste/trial-ended")
    }

    /// Shape only: pe1.<sealed>.<tag>. The server decides if it is good.
    static func plausible(_ token: String) -> Bool {
        let t = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard t.hasPrefix("pe1."), t.count > 40, t.count < 4000,
              t.range(of: #"^[A-Za-z0-9._-]+$"#, options: .regularExpression) != nil else { return false }
        return t.split(separator: ".").count == 3
    }

    static func read() -> Stored? {
        guard let data = try? Data(contentsOf: path),
              let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = doc["token"] as? String, plausible(token) else { return nil }
        let ends = (doc["ends"] as? NSNumber)?.doubleValue ?? 0
        return Stored(token: token, ends: Date(timeIntervalSince1970: ends), over: doc["over"] as? Bool ?? false)
    }

    @discardableResult
    static func save(_ token: String, ends: Double, over: Bool = false) -> Bool {
        let t = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard plausible(t) else { return false }
        let dir = path.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            var doc: [String: Any] = ["token": t, "ends": ends]
            if over { doc["over"] = true }
            let body = try JSONSerialization.data(withJSONObject: doc, options: [.sortedKeys])
            try body.write(to: path, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
            try? FileManager.default.removeItem(at: endedMark)
            return true
        } catch {
            return false
        }
    }

    /// The trial is over but a late payment can still bring the key: keep asking, stop drafting.
    static func markOver() {
        guard let s = read(), !s.over else { return }
        save(s.token, ends: s.ends.timeIntervalSince1970, over: true)
    }

    /// Delete the entitlement. `ended` remembers that a trial ran here, so the app says "trial ended", not "start".
    static func clear(ended: Bool = false) {
        try? FileManager.default.removeItem(at: path)
        if ended { try? Data("1".utf8).write(to: endedMark, options: .atomic) }
    }

    static func endedHere() -> Bool { FileManager.default.fileExists(atPath: endedMark.path) }

    /// The app's own view: on until the trial end plus a day for Stripe's first charge. The server decides
    /// for real on every draft. An unknown end (0) counts as on until the server says otherwise.
    static func on(_ stored: Stored?, now: Date = Date()) -> Bool {
        guard let stored, !stored.over else { return false }
        return stored.ends.timeIntervalSince1970 == 0 || now < stored.ends.addingTimeInterval(24 * 3600)
    }

    /// paisty://trial?e=…&end=… from the thanks page.
    static func fromURL(_ url: URL) -> (token: String, ends: Double)? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false), parts.scheme == PaistyLicense.scheme,
              parts.host == "trial" || parts.path == "trial" || parts.path == "/trial",
              let token = parts.queryItems?.first(where: { $0.name == "e" })?.value, plausible(token) else { return nil }
        let ends = Double(parts.queryItems?.first(where: { $0.name == "end" })?.value ?? "") ?? 0
        return (token, ends)
    }
}

/// One answer for "what may this Mac do". A valid license always wins; a running trial never shows key entry.
enum PaistyAccess: Equatable {
    case licensed   // a valid key
    case trial      // a running trial: no key, nothing to paste
    case ended      // a trial ran here and is over
    case expired    // a saved key that is no longer valid
    case none       // never set up: no key, no trial

    static func decide(licenseValid: Bool, licensePresent: Bool, trialOn: Bool, trialSeen: Bool) -> PaistyAccess {
        if licenseValid { return .licensed }
        if trialOn { return .trial }
        if trialSeen { return .ended }
        return licensePresent ? .expired : .none
    }

    static func current() -> PaistyAccess {
        let key = PaistyLicense.read()
        let ent = PaistyEntitlement.read()
        return decide(licenseValid: PaistyLicense.valid(key, device: PaistyLicense.deviceID), licensePresent: !key.isEmpty,
                      trialOn: PaistyEntitlement.on(ent), trialSeen: ent != nil || PaistyEntitlement.endedHere())
    }

    /// Key entry is for paid keys only, and never while a trial runs or a key works.
    static func showsKeyEntry(_ access: PaistyAccess) -> Bool {
        access == .ended || access == .expired || access == .none
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
