// Key activation, device binding, update schedule. Build:
// swiftc -O -framework IOKit -o /tmp/license-tests contrib/clip/License.swift contrib/clip/tests/license/main.swift
import Foundation

var failed = 0
var passed = 0
func check(_ ok: Bool, _ name: String) {
    if ok { passed += 1 } else { failed += 1; print("FAIL", name) }
}

func hexKey(_ claims: [String: Any]) -> String {
    let body = try! JSONSerialization.data(withJSONObject: claims, options: [.sortedKeys])
    return body.map { String(format: "%02x", $0) }.joined() + "." + String(repeating: "ab", count: 32)
}

let here = PaistyLicense.deviceHash(uuid: "test-hardware-one")
let there = PaistyLicense.deviceHash(uuid: "test-hardware-two")
check(here.count == 32 && here.allSatisfy { $0.isHexDigit }, "device hash is 32 hex")
check(here == PaistyLicense.deviceHash(uuid: "TEST-HARDWARE-ONE"), "device hash ignores case")
check(here != there, "different Macs differ")
check(!here.contains("hardware"), "hash does not echo the hardware id")

// Lifetime device key: valid forever on its own Mac, never on another.
let life = hexKey(["exp": 0, "plan": "lifetime", "sub": "own_x", "dev": here, "kid": "k1"])
check(PaistyLicense.valid(life, device: here), "lifetime valid here")
check(PaistyLicense.valid(life, device: here, now: Date(timeIntervalSince1970: 9_000_000_000)), "lifetime valid in 2255")
check(!PaistyLicense.valid(life, device: there), "lifetime refused on another Mac")
check(PaistyLicense.boundElsewhere(life, device: there), "boundElsewhere")
// Plain keys, trials, subscriptions.
check(PaistyLicense.valid(hexKey(["exp": 0, "plan": "lifetime", "sub": "cus_1"]), device: there), "unbound lifetime")
let past = Date().timeIntervalSince1970 - 100
check(!PaistyLicense.valid(hexKey(["exp": past, "plan": "trial", "sub": "trial_1"]), device: here), "ended trial")
check(PaistyLicense.valid(hexKey(["exp": past, "plan": "month", "sub": "cus_9"]), device: here), "lapsed sub defers to server renewal")
let p2 = "p2." + String(repeating: "A", count: 60) + "." + String(repeating: "b", count: 43)
check(PaistyLicense.valid(p2, device: here), "sealed p2 key accepted for the server to judge")
check(!PaistyLicense.valid("p2.short", device: here), "short p2 refused")
check(!PaistyLicense.valid("", device: here), "empty refused")
check(!PaistyLicense.plausible("hello world"), "junk not plausible")

// Activation links.
let enc = p2.addingPercentEncoding(withAllowedCharacters: .alphanumerics)!
check(PaistyLicense.activationKey(from: URL(string: "paisty://activate?k=" + enc)!) == p2, "paisty:// link")
check(PaistyLicense.activationKey(from: URL(string: "https://www.paisty.com/activate#k=" + enc)!) == p2, "https link with fragment")
check(PaistyLicense.activationKey(from: URL(string: "paisty://activate?k=bad")!) == nil, "bad key ignored")
check(PaistyLicense.activationKey(from: URL(string: "https://evil.example/activate#k=" + enc)!) == nil, "other host ignored")
check(PaistyLicense.activationKey(from: URL(string: "paisty://other?k=" + enc)!) == nil, "other action ignored")

// Feed headers carry the key and the device hash only when there is a key.
let h = PaistyLicense.feedHeaders(key: life, device: here)
check(h["X-Paisty-License"] == life && h["X-Paisty-Device"] == here, "feed headers")
check(PaistyLicense.feedHeaders(key: "", device: here).isEmpty, "no key, no headers")

// Claim schedule: soon after launch, then steady, never when a key exists.
let plan = PaistyLicense.claimSchedule(hasKey: false)
check(plan.first == 2, "first claim at 2 s")
check(plan.count > 60 && plan.last! <= 6 * 3600, "claims for the whole window")
check(zip(plan, plan.dropFirst()).allSatisfy { $0 < $1 }, "increasing")
check(PaistyLicense.claimSchedule(hasKey: true).isEmpty, "no claims with a key")

// Update schedule and relaunch rule.
check(UpdateSchedule.firstCheck <= 30, "checks soon after launch")
check(UpdateSchedule.every == 3 * 3600, "every 3 hours")
check(UpdateSchedule.due(lastCheck: nil), "due when never checked")
check(!UpdateSchedule.due(lastCheck: Date().addingTimeInterval(-600)), "not due 10 min later")
check(UpdateSchedule.due(lastCheck: Date().addingTimeInterval(-4 * 3600)), "due after 4 h asleep")
check(UpdateSchedule.underLaunchd(["XPC_SERVICE_NAME": "com.hailports.paiste"]), "launchd job")
check(!UpdateSchedule.underLaunchd(["XPC_SERVICE_NAME": "application.com.hailports.paiste.123.456"]), "Finder launch")
check(!UpdateSchedule.underLaunchd(["XPC_SERVICE_NAME": "0"]), "terminal launch")
check(!UpdateSchedule.underLaunchd([:]), "no env")

// Trials: an entitlement, never a key. Pure checks only: these tests never touch ~/.config/paiste.
let ent = "pe1." + String(repeating: "C", count: 60) + "." + String(repeating: "d", count: 43)
check(PaistyEntitlement.plausible(ent), "entitlement shape")
check(!PaistyLicense.plausible(ent), "an entitlement is not a license key")
check(!PaistyEntitlement.plausible(p2), "a license key is not an entitlement")
check(!PaistyEntitlement.plausible("pe1.short.x"), "short entitlement refused")
check(PaistyLicense.activationKey(from: URL(string: "paisty://activate?k=" + ent.addingPercentEncoding(withAllowedCharacters: .alphanumerics)!)!) == nil, "an entitlement never activates as a key")
let entLink = PaistyEntitlement.fromURL(URL(string: "paisty://trial?e=" + ent + "&end=1795000000")!)
check(entLink?.token == ent && entLink?.ends == 1795000000, "paisty://trial link")
check(PaistyEntitlement.fromURL(URL(string: "paisty://trial?e=" + p2)!) == nil, "a key in a trial link is refused")
check(PaistyEntitlement.fromURL(URL(string: "https://evil.example/trial?e=" + ent)!) == nil, "other scheme ignored")
check(PaistyEntitlement.fromURL(URL(string: "paisty://activate?e=" + ent)!) == nil, "other action ignored")
let endsAt = Date(timeIntervalSince1970: 1_800_000_000)
let running = PaistyEntitlement.Stored(token: ent, ends: endsAt, over: false)
check(PaistyEntitlement.on(running, now: endsAt.addingTimeInterval(-3600)), "on during the trial")
check(PaistyEntitlement.on(running, now: endsAt.addingTimeInterval(3600)), "on while the first charge is pending")
check(!PaistyEntitlement.on(running, now: endsAt.addingTimeInterval(2 * 24 * 3600)), "off two days after the end")
check(!PaistyEntitlement.on(PaistyEntitlement.Stored(token: ent, ends: endsAt, over: true), now: endsAt.addingTimeInterval(-3600)), "off once the server says over")
check(PaistyEntitlement.on(PaistyEntitlement.Stored(token: ent, ends: Date(timeIntervalSince1970: 0), over: false)), "unknown end: the server decides")
check(!PaistyEntitlement.on(nil), "no entitlement")

// Claim answers: a key is a key, a trial is a trial, and a dead trial says so.
check(PaistyLicense.readClaim(["ok": true, "token": p2]).result == .license, "claim: key")
let claimedTrial = PaistyLicense.readClaim(["ok": true, "entitlement": ent, "ends": 1795000000])
check(claimedTrial.result == .trial && claimedTrial.entitlement == ent && claimedTrial.key == nil && claimedTrial.ends == 1795000000, "claim: trial, never saved as a key")
check(PaistyLicense.readClaim(["ok": true, "token": ent]).result == .nothing, "claim: an entitlement in the key slot is ignored")
check(PaistyLicense.readClaim(["ok": false, "trial": "ended"]).result == .ended, "claim: ended")
check(PaistyLicense.readClaim(["ok": false, "trial": "past_due"]).result == .over, "claim: payment failed")
check(PaistyLicense.readClaim(["ok": false, "trial": "on"]).result == .nothing, "claim: still on")
check(PaistyLicense.trialEvery == 1800 && PaistyLicense.trialTail >= 7 * 24 * 3600, "trial poll cadence")

// Access: a valid key always wins; a running trial never shows key entry.
check(PaistyAccess.decide(licenseValid: true, licensePresent: true, trialOn: true, trialSeen: true) == .licensed, "lifetime key wins over a trial")
check(PaistyAccess.decide(licenseValid: false, licensePresent: true, trialOn: true, trialSeen: true) == .trial, "a trial beats an old expired key")
check(PaistyAccess.decide(licenseValid: false, licensePresent: false, trialOn: false, trialSeen: true) == .ended, "trial over")
check(PaistyAccess.decide(licenseValid: false, licensePresent: true, trialOn: false, trialSeen: false) == .expired, "expired key")
check(PaistyAccess.decide(licenseValid: false, licensePresent: false, trialOn: false, trialSeen: false) == .none, "fresh")
check(!PaistyAccess.showsKeyEntry(.trial) && !PaistyAccess.showsKeyEntry(.licensed), "no key entry during a trial or with a key")
check(PaistyAccess.showsKeyEntry(.ended) && PaistyAccess.showsKeyEntry(.expired) && PaistyAccess.showsKeyEntry(.none), "key entry for paid keys otherwise")

print("license tests: \(passed) passed, \(failed) failed")
exit(failed == 0 ? 0 : 1)
