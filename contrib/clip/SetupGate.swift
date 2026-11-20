import Foundation

// What launch shows. Pure logic, no AppKit, so tests compile it alone
// (contrib/clip/tests/setupgate/main.swift). A user who is already set up never gets the
// onboarding pages again just because an update dropped a privacy grant.

enum SetupStep: Equatable {
    case nothing   // set up and trusted
    case reenable  // set up, but Accessibility (or a Screen Recording grant they had) is gone
    case full      // never set up: the whole onboarding
}

enum SetupGate {
    /// Set up = route.yaml has at least one model, or a license key is saved.
    static func decide(routeHasModel: Bool, licensePresent: Bool, axTrusted: Bool,
                       screenWasGranted: Bool = false, screenGranted: Bool = true) -> SetupStep {
        guard routeHasModel || licensePresent else { return .full }
        if !axTrusted { return .reenable }
        if screenWasGranted && !screenGranted { return .reenable }
        return .nothing
    }

    /// True when the route text has a `models:` block with at least one entry.
    /// Matches the writer in hailports/config.py (two-space indents, empty map = bare `models:`).
    static func routeHasModel(_ text: String) -> Bool {
        let lines = text.components(separatedBy: "\n")
        var inside: Int? = nil
        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            let indent = line.prefix { $0 == " " }.count
            if let depth = inside {
                if indent > depth, trimmed.hasSuffix(":") || trimmed.contains(": ") { return true }
                if indent <= depth { inside = nil }
            }
            if inside == nil, trimmed == "models:" { inside = indent }
        }
        return false
    }

    static func routeHasModel(atPath path: String) -> Bool {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return false }
        return routeHasModel(text)
    }
}
