# paisty keys and updates

Nobody pastes a key.

- **Form → first launch.** `/api/trial` (and the checkout thanks page) issues the key, mails it, and
  parks it for that network for 6 hours. The app asks `/api/claim` 2 s after launch and then every
  20 s, then every 10 min, until it has a key. Single use.
- **Email link.** `https://www.paisty.com/activate#k=…` opens `paisty://activate?k=…`; the app saves it.
- **Reinstall / new Mac.** The key file (`~/.config/paiste/license`, 0600) survives a reinstall. On a wiped
  Mac, "Send my trial key" with the same email re-issues the paid key (or the live trial) and the app
  claims it.
- **Renewal.** Subscription keys carry the customer; past their date `/api/license` asks Stripe.
- **Device keys.** `{exp: 0, plan: lifetime, dev, kid}`. `dev` is a salted SHA-256 of the hardware UUID.
  Valid only on that Mac and only while KV `devkey:<kid>` is `active`. Revoke with
  `tools/device_key.py revoke --kid …` in the site repo.
- **Updates.** 20 s after launch, every 3 hours, and on wake. The feed opens for the baked token or a
  good license key, so a rotated token never strands a licensed Mac. The bundle is swapped in place
  and launchd's KeepAlive (or a detached `open`) brings the new one up.
  `paisty.app/Contents/MacOS/paisty --check-update` prints what the feed offers without installing.
- **Website.** `/dl` streams R2 `paisty.dmg`, which every release overwrites and CI reads back, so a new
  download is always the newest build.
- **Testing an update.** Run the release workflow by hand with `rebuild: 1`: the same commit goes out
  one build up (builds are commits × 10).
