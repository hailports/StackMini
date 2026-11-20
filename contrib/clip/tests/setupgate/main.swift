import Foundation

// swiftc -o /tmp/setupgate-tests contrib/clip/SetupGate.swift contrib/clip/tests/setupgate/main.swift && /tmp/setupgate-tests
var failures = 0
var count = 0
func expect(_ ok: @autoclosure () -> Bool, _ what: String, line: Int = #line) {
    count += 1
    if !ok() { failures += 1; print("FAIL line \(line): \(what)") }
}

// Set up + Accessibility lost to an update: only the small re-enable step.
expect(SetupGate.decide(routeHasModel: true, licensePresent: false, axTrusted: false) == .reenable, "set up, untrusted")
expect(SetupGate.decide(routeHasModel: false, licensePresent: true, axTrusted: false) == .reenable, "licensed, untrusted")
// Not set up: the whole onboarding, trusted or not.
expect(SetupGate.decide(routeHasModel: false, licensePresent: false, axTrusted: false) == .full, "fresh, untrusted")
expect(SetupGate.decide(routeHasModel: false, licensePresent: false, axTrusted: true) == .full, "fresh, trusted")
// Set up + trusted: nothing.
expect(SetupGate.decide(routeHasModel: true, licensePresent: true, axTrusted: true) == .nothing, "set up, trusted")
// Screen Recording only counts when they had granted it before.
expect(SetupGate.decide(routeHasModel: true, licensePresent: false, axTrusted: true,
                        screenWasGranted: false, screenGranted: false) == .nothing, "never granted screen")
expect(SetupGate.decide(routeHasModel: true, licensePresent: false, axTrusted: true,
                        screenWasGranted: true, screenGranted: false) == .reenable, "screen grant lost")

let route = """
paiste:
  enabled: true
  order:
    - cursor
  models:
    cursor:
      via: cursor
      model: cursor
llm:
  model: local
"""
expect(SetupGate.routeHasModel(route), "route with a model")
expect(!SetupGate.routeHasModel("paiste:\n  enabled: true\n  models:\nllm:\n  model: local\n"), "empty models map")
expect(!SetupGate.routeHasModel("paiste:\n  enabled: true\n  order: []\n"), "no models key")
expect(!SetupGate.routeHasModel(""), "empty file")
expect(!SetupGate.routeHasModel(atPath: "/nonexistent/route.yaml"), "missing file")

if failures > 0 {
    print("\(failures) of \(count) failed")
    exit(1)
}
print("\(count) passed")
