# paisty keys, trials and updates

Nobody pastes a key, and a trial has no key at all.

- **Trial (since 2026-11-21).** The trial starts on the website: email plus Stripe Checkout with a card, 7 days
  free, then $6/month unless cancelled. The thanks page starts the download and parks a signed trial
  entitlement (`pe1.…`, bound to the Stripe customer and subscription) for that network for 6 hours. The app asks
  `/api/claim` 2 s after launch and saves it to `~/.config/paiste/entitlement` (0600). It is never written to the
  license file, never shown, never pasted. `paisty://trial?e=…&end=…` from the thanks page does the same on any
  network. The app shows no key entry while a trial runs.
- **During the trial.** The draft gate (`hailports/clip/gate.py`) sends `{"entitlement": …}` to `/api/license`
  (and `/api/prompt`, `/api/setup`) when there is no working license. The server reads the live subscription.
  A yes is cached 15 minutes; offline, a trial checked within the last day keeps drafting until a day after its
  end.
- **Day 8.** The first paid invoice makes a normal month key on the server. The app asks every 30 minutes with its
  entitlement, saves the key as a normal license, and deletes the entitlement. The key is also mailed once as a
  backup. A cancelled trial or a failed first charge gets no key; the app stops with the trial and says
  "Your trial ended". A late payment still brings the key while the app keeps asking (14 days after the end).
- **Paid checkout → first launch.** The checkout thanks page issues the key, mails it, and parks it for that
  network for 6 hours. The app asks `/api/claim` 2 s after launch, then every 20 s, then every 10 min, until it
  has a key or a trial. Single use.
- **Email link.** `https://www.paisty.com/activate#k=…` opens `paisty://activate?k=…`; the app saves the key.
- **Reinstall / new Mac.** The key file (`~/.config/paiste/license`, 0600) survives a reinstall. On a wiped Mac, a
  buyer who uses the download form with the same email gets the paid key again (capped) and the app claims it.
  During a running trial the form mails that address a link to its thanks page, which sets the new Mac up.
- **A working key always wins.** A valid license (lifetime keys have `exp: 0` and never expire) is never replaced
  by a trial, and a trial never writes the license file.
- **Renewal.** Subscription keys carry the customer; past their date `/api/license` asks Stripe. Only paid,
  Paisty-product subscriptions count; trialing never renews a key.
- **Device keys.** `{exp: 0, plan: lifetime, dev, kid}`. `dev` is a salted SHA-256 of the hardware UUID.
  Valid only on that Mac and only while KV `devkey:<kid>` is `active`. Revoke with
  `tools/device_key.py revoke --kid …` in the site repo.
- **Updates.** 20 s after launch, every 3 hours, and on wake. The feed opens for the baked token or a
  good license key, so a rotated token never strands a licensed Mac. Trial copies use the baked token. The bundle
  is swapped in place and launchd's KeepAlive (or a detached `open`) brings the new one up.
  `paisty.app/Contents/MacOS/paisty --check-update` prints what the feed offers without installing.
- **Website.** `/dl` streams R2 `paisty.dmg`, which every release overwrites and CI reads back, so a new
  download is always the newest build. Ship the app before the site's trial change: older copies cannot use an
  entitlement.
- **Testing an update.** Run the release workflow by hand with `rebuild: 1`: the same commit goes out
  one build up (builds are commits × 10).
